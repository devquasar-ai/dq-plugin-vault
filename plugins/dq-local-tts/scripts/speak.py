#!/usr/bin/env python3
"""Client for the Kokoro TTS daemon. Stdlib only, so any python3 can run it.

Autostarts the daemon (inside its own venv) on the first request.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ttslib  # noqa: E402

HERE = Path(__file__).resolve().parent


def connect(timeout=5):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(ttslib.SOCKET))
    return sock


def start_daemon(wait=60.0):
    python = ttslib.VENV_PYTHON if ttslib.VENV_PYTHON.exists() else Path(sys.executable)
    ttslib.HOME.mkdir(parents=True, exist_ok=True)
    log = open(ttslib.LOGFILE, "a", buffering=1)
    proc = subprocess.Popen(
        [str(python), str(HERE / "daemon.py")],
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            connect(2).close()
            return True
        except OSError:
            pass
        if proc.poll() is not None:   # died on startup; don't wait out the timeout
            return False
        time.sleep(0.2)
    return False


def _startup_error() -> str:
    try:
        tail = ttslib.LOGFILE.read_text(encoding="utf-8", errors="replace").splitlines()[-6:]
    except OSError:
        tail = []
    detail = "\n  ".join(tail)
    return f"TTS daemon failed to start (log: {ttslib.LOGFILE})" + (f"\n  {detail}" if detail else "")


def request(payload, autostart=True):
    try:
        sock = connect()
    except OSError:
        if not autostart:
            return {"ok": False, "error": "daemon not running", "running": False}
        if not start_daemon():
            return {"ok": False, "error": _startup_error()}
        try:
            sock = connect()
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
    with sock:
        sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        data = b""
        try:
            while not data.endswith(b"\n"):
                part = sock.recv(65536)
                if not part:
                    break
                data += part
        except socket.timeout:
            return {"ok": False, "error": "daemon timed out"}
    try:
        return json.loads(data.decode("utf-8"))
    except ValueError:
        return {"ok": False, "error": "bad response from daemon"}


def main():
    parser = argparse.ArgumentParser(description="Speak text with the local Kokoro daemon")
    parser.add_argument("text", nargs="*", help="text to speak (default: stdin)")
    parser.add_argument("--voice", default=None)
    parser.add_argument("--speed", type=float, default=None)
    parser.add_argument("--raw", action="store_true", help="skip markdown cleaning")
    parser.add_argument("--max-chars", type=int, default=None)
    parser.add_argument("--stop", action="store_true", help="cancel playback and clear the queue")
    parser.add_argument("--ping", action="store_true", help="report daemon status")
    parser.add_argument("--shutdown", action="store_true", help="stop the daemon")
    parser.add_argument("--start", action="store_true", help="start (and preload) the daemon")
    args = parser.parse_args()

    if args.stop:
        print(json.dumps(request({"cmd": "stop"}, autostart=False)))
        return 0
    if args.shutdown:
        print(json.dumps(request({"cmd": "shutdown"}, autostart=False)))
        return 0
    if args.ping:
        print(json.dumps(request({"cmd": "ping"}, autostart=False)))
        return 0
    if args.start:
        os.environ["LOCAL_TTS_PRELOAD"] = "1"
        print(json.dumps(request({"cmd": "ping"})))
        return 0

    cfg = ttslib.load_config()
    text = " ".join(args.text) if args.text else sys.stdin.read()
    if not args.raw:
        text = ttslib.clean_for_tts(text, args.max_chars if args.max_chars is not None else cfg["max_chars"])
    if not text.strip():
        return 0

    resp = request({"cmd": "speak", "text": text,
                    "voice": args.voice or cfg["voice"],
                    "speed": args.speed if args.speed is not None else cfg["speed"]})
    if not resp.get("ok"):
        print(resp.get("error", "failed"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
