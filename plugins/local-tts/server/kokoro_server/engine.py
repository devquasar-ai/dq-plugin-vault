"""Kokoro model wrapper: text in, WAV bytes out.

Deliberately the whole of the audio concern. Nothing here knows about HTTP,
and nothing here plays sound -- a container has no audio device, so callers
receive bytes and play them on the host.
"""
import io
import logging
import threading

import numpy as np
import soundfile as sf

SAMPLE_RATE = 24000

log = logging.getLogger(__name__)

VOICES = {
    "a": ["af_heart", "af_bella", "af_nicole", "af_aoede", "af_kore", "af_sarah",
          "af_nova", "af_sky", "af_alloy", "af_jessica", "af_river",
          "am_michael", "am_fenrir", "am_puck", "am_echo", "am_eric",
          "am_liam", "am_onyx", "am_santa", "am_adam"],
    "b": ["bf_emma", "bf_isabella", "bf_alice", "bf_lily",
          "bm_george", "bm_fable", "bm_lewis", "bm_daniel"],
}
ALL_VOICES = VOICES["a"] + VOICES["b"]


def lang_for(voice: str) -> str:
    """Kokoro pipelines are per-language, and the voice prefix encodes it."""
    return "b" if voice.startswith(("bf_", "bm_")) else "a"


class Engine:
    """Holds one KPipeline per language, built on first use.

    Kokoro is not documented as thread-safe, so synthesis is serialized. That
    is not a real constraint here: a single GPU/CPU generates one stream at a
    time anyway, and clients pipeline by sending the next chunk early.
    """

    def __init__(self, preload: str = ""):
        self._pipelines = {}
        self._lock = threading.Lock()
        for lang in filter(None, preload.split(",")):
            self.pipeline(lang.strip())

    def pipeline(self, lang: str):
        with self._lock:
            if lang not in self._pipelines:
                from kokoro import KPipeline
                log.info("loading Kokoro pipeline for lang_code=%s", lang)
                self._pipelines[lang] = KPipeline(lang_code=lang)
                log.info("pipeline %s ready", lang)
            return self._pipelines[lang]

    @property
    def loaded(self):
        return sorted(self._pipelines)

    def synthesize(self, text: str, voice: str, speed: float = 1.0) -> np.ndarray:
        if voice not in ALL_VOICES:
            raise ValueError(f"unknown voice {voice!r}")
        pipeline = self.pipeline(lang_for(voice))
        with self._lock:
            parts = [np.asarray(audio, dtype=np.float32)
                     for _, _, audio in pipeline(text, voice=voice, speed=speed)]
        if not parts:
            raise ValueError("model produced no audio for this text")
        return np.concatenate(parts)

    def synthesize_bytes(self, text: str, voice: str, speed: float = 1.0,
                         fmt: str = "wav") -> bytes:
        audio = self.synthesize(text, voice, speed)
        buf = io.BytesIO()
        if fmt == "pcm":                       # raw 16-bit LE, no container
            buf.write((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        else:
            sf.write(buf, audio, SAMPLE_RATE, format=fmt.upper())
        return buf.getvalue()
