"""Synthesis backends: text in, WAV bytes out.

Every backend hides the same thing -- how audio gets made -- and nothing else.
Filtering, chunking and playback live outside, so they work identically no
matter which backend is selected.
"""
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ttslib  # noqa: E402


# The contract this plugin speaks. Must match the server's /health api_version.
API_VERSION = 1
SERVICE = "kokoro-tts-server"

# Kokoro voice names: af_heart, bm_george, ... (lang + gender prefix).
KOKORO_VOICE = re.compile(r"^[ab][fm]_\w+$")


class BackendError(RuntimeError):
    """Raised with a message meant for the user, not a stack trace."""


class EmbeddedBackend:
    """Kokoro loaded in this process. Fastest on Apple silicon, but needs the
    ~1GB venv from setup.sh."""

    name = "embedded"

    def __init__(self, cfg):
        self.cfg = cfg
        self._pipelines = {}

    @staticmethod
    def check():
        import importlib.util
        missing = [m for m in ("numpy", "soundfile", "kokoro")
                   if importlib.util.find_spec(m) is None]
        if missing:
            raise BackendError(
                f"missing dependencies: {', '.join(missing)}. "
                f"Run setup.sh to build the venv at {ttslib.VENV}")

    def describe(self):
        return f"embedded (venv {ttslib.VENV})"

    def _pipeline(self, lang):
        if lang not in self._pipelines:
            from kokoro import KPipeline
            self._pipelines[lang] = KPipeline(lang_code=lang)
        return self._pipelines[lang]

    def warm(self):
        self._pipeline(self.cfg["lang_code"])

    def synthesize(self, text, voice, speed):
        import io
        import numpy as np
        import soundfile as sf

        lang = "b" if voice.startswith(("bf_", "bm_")) else "a"
        parts = [np.asarray(audio, dtype=np.float32)
                 for _, _, audio in self._pipeline(lang)(text, voice=voice, speed=speed)]
        if not parts:
            return b""
        buf = io.BytesIO()
        sf.write(buf, np.concatenate(parts), ttslib.SAMPLE_RATE, format="WAV")
        return buf.getvalue()


class HttpBackend:
    """A kokoro-tts-server (or any OpenAI-compatible /v1/audio/speech service).

    Needs no Python dependencies on this machine -- the model lives in the
    container, and only playback happens here.
    """

    name = "http"

    def __init__(self, cfg):
        self.cfg = cfg
        self.url = cfg["server_url"].rstrip("/")
        self.timeout = cfg["request_timeout"]

    def check(self):
        """Reachable *and* speaking a contract we understand.

        A kokoro-tts-server identifies itself and its API version, so a
        mismatch reports as such instead of failing later on a 422. Any other
        OpenAI-compatible endpoint is allowed through with a warning -- being
        usable with third-party servers is the point of this backend.
        """
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=5) as resp:
                info = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise BackendError(
                f"cannot reach a TTS server at {self.url} ({exc.reason}). "
                f"Start one with: tts-ctl.sh server up") from None
        except Exception:
            return {"compatible": "unknown",
                    "note": f"{self.url} has no recognizable /health; "
                            f"assuming an OpenAI-compatible speech endpoint"}

        if info.get("service") != SERVICE:
            return {"compatible": "unknown",
                    "note": f"{self.url} is not a {SERVICE}; assuming "
                            f"OpenAI-compatible /v1/audio/speech"}

        served = info.get("api_version")
        if served != API_VERSION:
            raise BackendError(
                f"{SERVICE} at {self.url} speaks API v{served}, but this plugin "
                f"speaks v{API_VERSION}. Update whichever is older.")
        return {"compatible": "yes", "server_version": info.get("version"),
                "loaded": info.get("loaded_pipelines") or []}

    def describe(self):
        return f"http ({self.url})"

    def warm(self):
        self.check()

    def synthesize(self, text, voice, speed):
        payload = json.dumps({
            "input": text, "voice": voice, "speed": speed,
            "model": "kokoro", "response_format": "wav",
            "clean": False,   # the plugin already filtered; never do it twice
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}/v1/audio/speech", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise BackendError(f"TTS server returned {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise BackendError(f"TTS server unreachable: {exc.reason}") from None


class SayBackend:
    """macOS's built-in `say`. No model, no venv, no container -- the system
    voices render straight to WAV, so it works on any Mac out of the box.

    Kokoro voice names mean nothing to `say`, so those fall back to the
    configured `say_voice` (empty = the system default voice).
    """

    name = "say"
    BASE_WPM = 175   # `say`'s typical default rate; speed scales it

    def __init__(self, cfg):
        self.cfg = cfg
        self.timeout = cfg["request_timeout"]

    @staticmethod
    def check():
        import shutil
        if sys.platform != "darwin" or not shutil.which("say"):
            raise BackendError("the 'say' backend needs macOS and its built-in `say` command")

    def describe(self):
        return f"say ({self.cfg.get('say_voice') or 'system default voice'})"

    def warm(self):
        pass

    def voice_for(self, voice):
        if not voice or KOKORO_VOICE.match(voice):
            return self.cfg.get("say_voice") or ""
        return voice

    def synthesize(self, text, voice, speed):
        import subprocess
        import tempfile

        voice = self.voice_for(voice)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "say.wav"
            cmd = ["say", "-o", str(out), "--file-format=WAVE",
                   f"--data-format=LEI16@{ttslib.SAMPLE_RATE}",
                   "-r", str(round(self.BASE_WPM * speed))]
            if voice:
                cmd += ["-v", voice]
            # Text goes in on stdin so a leading "-" is never read as a flag.
            proc = subprocess.run(cmd, input=text.encode("utf-8"),
                                  capture_output=True, timeout=self.timeout)
            if proc.returncode != 0:
                detail = proc.stderr.decode("utf-8", "replace").strip()[:300]
                raise BackendError(f"say failed: {detail or proc.returncode}")
            return out.read_bytes() if out.exists() else b""


BACKENDS = {"embedded": EmbeddedBackend, "http": HttpBackend, "say": SayBackend}


def build(cfg):
    name = cfg.get("backend", "embedded")
    if name not in BACKENDS:
        raise BackendError(f"unknown backend {name!r}; expected one of {', '.join(BACKENDS)}")
    backend = BACKENDS[name](cfg)
    result = backend.check()
    if isinstance(result, dict):
        backend.compat = result.get("note") or (
            f"{SERVICE} v{result.get('server_version')} API v{API_VERSION}, ok")
    return backend
