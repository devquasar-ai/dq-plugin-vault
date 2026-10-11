# kokoro-tts-server

The speech engine behind the [`local-tts`](../) plugin's `http` backend, and a
usable service in its own right. Nothing here knows the plugin exists.

A small, self-contained HTTP service that turns text into speech with
[Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M), running entirely
locally in Docker.

The contract is narrow on purpose: **text in, audio bytes out.**

## Why it does not play the audio

A container has no sound device. On macOS, Docker runs inside a Linux VM with
no CoreAudio access and no `/dev/snd` to pass through, so a container
physically cannot make noise. Playback therefore belongs to the caller:

```
container:  text ──► synthesize ──► WAV bytes over HTTP
host:       receive bytes ──► play (afplay / paplay / aplay)
```

This is also why the API is stateless — it hands back bytes and forgets you.

## Run it

```sh
docker compose up -d          # or: docker build -t kokoro-tts-server . && docker run -p 42821:8080 kokoro-tts-server
curl localhost:42821/health
```

The model is baked into the image, so a container start is not a download.
First request loads the pipeline (~4 s); set `KOKORO_PRELOAD=a` (the default)
to do that at startup instead.

## API

### `POST /v1/audio/speech` — OpenAI-compatible

The main endpoint. Any client written against OpenAI's speech API works.

```sh
curl -X POST localhost:42821/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"input":"Hello from the container.","voice":"af_heart"}' \
  --output hello.wav
```

| Field | Default | Notes |
|---|---|---|
| `input` | required | Text to speak |
| `voice` | `af_heart` | See `GET /v1/voices` |
| `speed` | `1.0` | 0.25–4.0 |
| `response_format` | `wav` | `wav`, `flac`, `ogg`, `pcm` |
| `clean` | `false` | Apply the text filter server-side |
| `max_chars` | `0` | Truncate at a sentence boundary; `0` disables |

`clean` defaults to **false** so a client that already filters — like the
`local-tts` plugin — never has it applied twice.

### `POST /v1/text/clean` — strip what should not be read aloud

Drops fenced and indented code, tables, diffs, tracebacks, HTML, shell
prompts and symbol-dense lines, then reduces markdown to prose and collapses
long paths to their basename. It also drops a dangling lead-in
("Here is the diff:") whose content it just removed.

### `POST /v1/text/split` — sentence chunking

Splits into speakable chunks with a deliberately **short first chunk**, so a
client can start playing while the rest is still being synthesized. Knows not
to break on `e.g.`, `i.e.`, `Dr.` and friends.

These two are offered for other clients and the planned MCP surface. They are
not required: a client that prefers to filter and split locally (so the same
code serves a non-HTTP backend too) can ignore them and just loop over
`/v1/audio/speech`, which is what `local-tts` does.

### `GET /health`

```json
{"status":"ok","service":"kokoro-tts-server","api_version":1,
 "version":"0.1.0","loaded_pipelines":["a"],"voice_count":28,"sample_rate":24000}
```

`service` and `api_version` let a client distinguish "server is down" from
"server speaks a contract I don't understand".

### `GET /v1/voices`

28 voices; `af_*`/`am_*` are American English, `bf_*`/`bm_*` British. The
prefix selects the pipeline, so no language field is needed.

## Performance note

Docker on macOS is **CPU-only** — no Metal passthrough. Running Kokoro
natively on the host is faster, and MLX faster still. Use this image when you
want a portable, isolated service shared by several tools; use a native
backend when you want the lowest latency on one machine.

## Tests

```sh
cd plugins/local-tts/server && python3 -m pytest tests/ -q
```

The text engine has no model dependency, which is what makes it worth testing.
CI runs these before publishing the image.

## Published image

`ghcr.io/csabakecskemeti/kokoro-tts-server:latest`, built for
`linux/amd64` and `linux/arm64` — arm64 so Apple silicon runs it natively
rather than emulated. The plugin pulls this, and falls back to building this
directory if the pull fails.

## Roadmap

- MCP surface over the same engine (`speak`, `clean_text`, `split_text`, `list_voices`)
- Streaming response for `/v1/audio/speech`
- Optional MLX backend for native (non-container) deployment
