FROM python:3.11-slim

# espeak-ng is Kokoro's phonemizer; libsndfile backs soundfile.
RUN apt-get update && apt-get install -y --no-install-recommends \
        espeak-ng libsndfile1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the CPU build explicitly: the default wheel drags in CUDA, which is
# dead weight everywhere this image can actually run (no GPU passthrough on
# macOS, and arm64 has no CUDA at all).
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY kokoro_server ./kokoro_server

# Bake the model AND every voice tensor into the image. Without the voices,
# the first request for a given voice silently downloads it from HuggingFace,
# which defeats the point of a local service and fails on an offline host.
ENV HF_HOME=/models
RUN python - <<'PY'
from kokoro import KPipeline
from kokoro_server.engine import VOICES
for lang, names in VOICES.items():
    pipeline = KPipeline(lang_code=lang)
    for name in names:
        pipeline.load_voice(name)
    print(f"baked {len(names)} voices for lang_code={lang}")
PY

# Proof the bake worked: any runtime fetch now fails loudly instead of
# quietly reaching the network.
ENV HF_HUB_OFFLINE=1
ENV KOKORO_PRELOAD=a
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD curl -fsS http://localhost:8080/health || exit 1

CMD ["uvicorn", "kokoro_server.app:app", "--host", "0.0.0.0", "--port", "8080"]
