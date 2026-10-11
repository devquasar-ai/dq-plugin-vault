"""HTTP surface for the Kokoro TTS server.

The contract is deliberately narrow: text in, audio bytes out. Playback is the
caller's job -- a container has no sound device. `/v1/audio/speech` follows
OpenAI's shape so existing TTS clients work unmodified.
"""
import logging
import os

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from . import textproc
from .engine import ALL_VOICES, SAMPLE_RATE, VOICES, Engine

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

MEDIA_TYPES = {"wav": "audio/wav", "flac": "audio/flac",
               "ogg": "audio/ogg", "pcm": "audio/L16"}

app = FastAPI(title="kokoro-tts-server", version="0.1.0",
              description="Local Kokoro text-to-speech over HTTP.")
engine = Engine(preload=os.environ.get("KOKORO_PRELOAD", ""))


class SpeechRequest(BaseModel):
    """OpenAI /v1/audio/speech, plus optional server-side text preparation."""
    input: str
    model: str = "kokoro"
    voice: str = "af_heart"
    speed: float = Field(1.0, ge=0.25, le=4.0)
    response_format: str = "wav"
    # Off by default: clients that already filter (like the local-tts plugin)
    # must not have it applied twice.
    clean: bool = False
    max_chars: int = 0


class CleanRequest(BaseModel):
    text: str
    max_chars: int = 0          # 0 = no limit


class SplitRequest(BaseModel):
    text: str
    first_chunk_chars: int = textproc.FIRST_CHUNK_CHARS
    chunk_chars: int = textproc.CHUNK_CHARS


# Bumped only on a breaking change to the request/response contract, so a
# client can tell "this server is too new/old for me" from "server is down".
API_VERSION = 1
SERVICE = "kokoro-tts-server"


@app.get("/health")
def health():
    return {"status": "ok",
            "service": SERVICE,
            "api_version": API_VERSION,
            "version": app.version,
            "loaded_pipelines": engine.loaded,
            "voice_count": len(ALL_VOICES),
            "sample_rate": SAMPLE_RATE}


@app.get("/v1/voices")
def voices():
    return {"voices": ALL_VOICES,
            "by_language": {"american_english": VOICES["a"],
                            "british_english": VOICES["b"]}}


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": "kokoro", "object": "model",
                                        "owned_by": "hexgrad"}]}


@app.post("/v1/text/clean")
def clean(req: CleanRequest):
    """Strip code, tables, diffs, HTML and paths. Offered for other clients;
    the local-tts plugin does this itself so it works with every backend."""
    return {"text": textproc.clean_for_tts(req.text, req.max_chars)}


@app.post("/v1/text/split")
def split(req: SplitRequest):
    """Sentence-chunk text, shortest chunk first for low time-to-first-audio."""
    chunks = textproc.split_chunks(req.text, req.first_chunk_chars, req.chunk_chars)
    return {"chunks": chunks, "count": len(chunks)}


@app.post("/v1/audio/speech")
def speech(req: SpeechRequest):
    fmt = req.response_format.lower()
    if fmt not in MEDIA_TYPES:
        raise HTTPException(400, f"unsupported response_format {req.response_format!r}; "
                                 f"expected one of {', '.join(MEDIA_TYPES)}")
    if req.voice not in ALL_VOICES:
        raise HTTPException(400, f"unknown voice {req.voice!r}; see GET /v1/voices")

    text = textproc.clean_for_tts(req.input, req.max_chars) if req.clean else req.input
    if not text.strip():
        raise HTTPException(400, "input is empty after preparation")

    try:
        audio = engine.synthesize_bytes(text, req.voice, req.speed, fmt)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:                      # model failure, not client error
        log.exception("synthesis failed")
        raise HTTPException(500, f"synthesis failed: {exc}")

    return Response(content=audio, media_type=MEDIA_TYPES[fmt],
                    headers={"X-Sample-Rate": str(SAMPLE_RATE)})
