#!/usr/bin/env python3
"""Warm Kokoro TTS daemon.

Loading torch + Kokoro costs several seconds, which is unacceptable per
reply, so the model lives in a long-running process behind a unix socket.
Clients enqueue text and return immediately; a single worker thread
synthesizes and plays so replies never overlap.
"""
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backends  # noqa: E402
import ttslib  # noqa: E402

IDLE_EXIT_SECONDS = 3 * 60 * 60  # let a forgotten daemon reclaim its ~1GB


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def find_player():
    for cand in (["afplay"], ["paplay"], ["aplay", "-q"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]):
        if shutil.which(cand[0]):
            return cand
    return None


class Speaker:
    """Two-stage pipeline: a synth thread fills a small queue of ready wav
    files, a playback thread drains it. Synthesis of chunk N+1 overlaps with
    playback of chunk N, so time-to-first-audio is one short sentence rather
    than the whole reply."""

    def __init__(self, cfg, backend):
        self.cfg = cfg
        self.backend = backend
        self.jobs = queue.Queue()
        self.ready = queue.Queue(maxsize=3)   # bounded: stay ~3 chunks ahead
        self.warmed = False
        self.player = find_player()
        self.proc = None
        self.proc_lock = threading.Lock()
        self.generation = 0                   # bumped by stop() to void in-flight work
        self.last_activity = time.time()
        if not self.player:
            log("WARNING: no audio player found (afplay/paplay/aplay/ffplay)")

    def warm(self):
        if not self.warmed:
            log("warming backend ...")
            t0 = time.time()
            self.backend.warm()
            self.warmed = True
            log(f"backend ready in {time.time() - t0:.1f}s")

    def enqueue(self, text, voice, speed):
        self.last_activity = time.time()
        self.jobs.put((self.generation, text, voice, speed))
        return self.jobs.qsize()

    def stop(self):
        self.generation += 1                  # everything in flight is now stale
        dropped = _drain(self.jobs)
        for path in _drain(self.ready, collect=True):
            if path:
                Path(path).unlink(missing_ok=True)
        with self.proc_lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        return dropped

    # --- synthesis stage --------------------------------------------------
    def run_synth(self):
        while True:
            try:
                gen, text, voice, speed = self.jobs.get(timeout=60)
            except queue.Empty:
                if time.time() - self.last_activity > IDLE_EXIT_SECONDS:
                    log("idle timeout, exiting")
                    os._exit(0)
                continue
            try:
                if gen == self.generation:
                    self._synth_job(gen, text, voice, speed)
            except Exception as exc:          # a bad voice must not kill the daemon
                log(f"ERROR: {exc}")
            finally:
                self.jobs.task_done()
                self.last_activity = time.time()

    def _synth_job(self, gen, text, voice, speed):
        self.warm()
        chunks = ttslib.split_chunks(text)
        label = getattr(self.backend, "voice_for", lambda v: v)(voice) or "default"
        log(f"speak [{label} x{speed}] {len(chunks)} chunk(s): "
            f"{text[:60]}{'...' if len(text) > 60 else ''}")
        t0 = time.time()

        for i, chunk in enumerate(chunks):
            if gen != self.generation:
                return
            try:
                audio = self.backend.synthesize(chunk, voice, speed)
            except backends.BackendError as exc:
                log(f"ERROR: {exc}")
                return
            if not audio or gen != self.generation:
                continue
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(audio)
                path = tmp.name
            if i == 0:
                log(f"  first audio in {time.time() - t0:.1f}s")
            # put() blocks once we are 3 chunks ahead, which is the backpressure
            # that keeps a long reply from synthesizing itself into memory.
            self.ready.put((gen, path))

    # --- playback stage ---------------------------------------------------
    def run_playback(self):
        while True:
            gen, path = self.ready.get()
            try:
                if gen != self.generation or not self.player:
                    continue
                with self.proc_lock:
                    self.proc = subprocess.Popen(self.player + [path],
                                                 stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.DEVNULL)
                self.proc.wait()
            except Exception as exc:
                log(f"ERROR (playback): {exc}")
            finally:
                with self.proc_lock:
                    self.proc = None
                Path(path).unlink(missing_ok=True)
                self.ready.task_done()
                self.last_activity = time.time()


def _drain(q, collect=False):
    """Empty a queue; return the item count, or the items when collecting."""
    items = []
    while True:
        try:
            item = q.get_nowait()
        except queue.Empty:
            break
        q.task_done()
        items.append(item[-1] if collect and isinstance(item, tuple) else item)
    return items if collect else len(items)


def handle(conn, speaker, cfg):
    with conn:
        conn.settimeout(5)
        data = b""
        while not data.endswith(b"\n"):
            part = conn.recv(65536)
            if not part:
                break
            data += part
        if not data.strip():
            return
        try:
            req = json.loads(data.decode("utf-8"))
        except ValueError as exc:
            conn.sendall(json.dumps({"ok": False, "error": f"bad json: {exc}"}).encode() + b"\n")
            return

        cmd = req.get("cmd", "speak")
        if cmd == "ping":
            resp = {"ok": True, "loaded": speaker.warmed,
                    "version": ttslib.plugin_version(),
                    "queued": speaker.jobs.qsize(), "pid": os.getpid(),
                    "backend": speaker.backend.describe()}
        elif cmd == "stop":
            resp = {"ok": True, "dropped": speaker.stop()}
        elif cmd == "shutdown":
            conn.sendall(json.dumps({"ok": True}).encode() + b"\n")
            log("shutdown requested")
            os._exit(0)
        elif cmd == "speak":
            text = (req.get("text") or "").strip()
            if not text:
                resp = {"ok": False, "error": "empty text"}
            else:
                resp = {"ok": True, "queued": speaker.enqueue(
                    text,
                    req.get("voice") or cfg["voice"],
                    float(req.get("speed") or cfg["speed"]),
                )}
        else:
            resp = {"ok": False, "error": f"unknown cmd: {cmd}"}
        conn.sendall(json.dumps(resp).encode() + b"\n")




def main():
    ttslib.HOME.mkdir(parents=True, exist_ok=True)
    cfg = ttslib.load_config()

    # Refuse to listen at all when we could not possibly synthesize: a daemon
    # that accepts text and silently fails is worse than one that never starts.
    try:
        backend = backends.build(cfg)
    except backends.BackendError as exc:
        log(f"FATAL: {exc}")
        sys.exit(1)
    log(f"backend: {backend.describe()}")
    note = getattr(backend, "compat", None)
    if note:
        log(f"compatibility: {note}")

    if ttslib.SOCKET.exists():
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(ttslib.SOCKET))
            probe.close()
            log("another daemon is already listening, exiting")
            return
        except OSError:
            ttslib.SOCKET.unlink(missing_ok=True)  # stale socket from a crash
        finally:
            probe.close()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(ttslib.SOCKET))
    ttslib.SOCKET.chmod(0o600)
    server.listen(16)
    ttslib.PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
    log(f"listening on {ttslib.SOCKET} (pid {os.getpid()})")

    speaker = Speaker(cfg, backend)
    threading.Thread(target=speaker.run_synth, daemon=True).start()
    threading.Thread(target=speaker.run_playback, daemon=True).start()
    if os.environ.get("LOCAL_TTS_PRELOAD") == "1":
        threading.Thread(target=speaker.warm, daemon=True).start()

    try:
        while True:
            conn, _ = server.accept()
            threading.Thread(target=handle, args=(conn, speaker, cfg), daemon=True).start()
    finally:
        ttslib.SOCKET.unlink(missing_ok=True)
        ttslib.PIDFILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
