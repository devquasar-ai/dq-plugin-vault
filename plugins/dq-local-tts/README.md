# dq-local-tts

*DQ = [DevQuasar](https://devquasar.com).*

A Claude Code plugin that reads Claude's replies out loud — with
[Kokoro](https://huggingface.co/hexgrad/Kokoro-82M), or with macOS's built-in
`say` voices. Everything runs on your machine — no API key, and no network call
when it speaks.

## How it works

Three stages, with a clear split of labour:

```
 ┌─ this machine ──────────────────────────────────────────────┐
 │                                                             │
 │  PreToolUse ─┐   hook worker           daemon               │
 │  Stop ───────┴─► · new text this turn ─► · split sentences  │
 │  (detached,      · dedupe               · synth one ahead ──┼──► backend
 │   ~0.3 s)        · filter code/tables   · play the audio ◄──┼─── WAV bytes
 │                                                             │
 └─────────────────────────────────────────────────────────────┘
   backend = embedded (Kokoro in the daemon)
           | http     (kokoro-tts-server container on :42821)
           | say      (macOS `say`)
```

**The hooks decide what to say. The daemon splits and plays. The backend only
synthesizes.** Because a backend just turns text into WAV bytes, swapping it
changes nothing about what is spoken or how it is played.

Playback can never move into the container: on macOS Docker runs inside a Linux
VM with no CoreAudio access and no `/dev/snd`, so a container physically cannot
make sound. It hands back bytes.

Three things make it usable turn after turn:

**The hook never blocks.** It buffers the payload, hands it to a detached
worker, and returns in ~0.3 s. A turn never waits on synthesis or playback.
Text written before a tool call is spoken as that tool starts (`PreToolUse`),
so a long, tool-heavy turn talks as it goes instead of all at the end.

**Sentence streaming.** Kokoro emits nothing until it has synthesized a whole
passage, so a long answer would mean a long silence. `split_chunks()` breaks
text into sentence-sized pieces — a deliberately short one first — and requests
chunk *N+1* while chunk *N* is still playing.

**An aggressive filter.** Code, tables, diffs, tracebacks, HTML and shell
transcripts are unlistenable. `strip_unspeakable()` drops them by *line* before
any markdown stripping, and also drops a dangling lead-in ("Here is the diff:")
whose content was just removed. Long paths collapse to their basename. So this:

````markdown
## Fixed
The off-by-one was in `client.py`:
```python
for i in range(retries): backoff(i)
```
| file | change |
|---|---|
It now stops after **three** attempts.
````

is spoken as: *"Fixed. It now stops after three attempts."*

Filtering and splitting live in the plugin rather than the server, so they work
identically on every backend — including `embedded` and `say`, which have no
server to ask.

## Backends

| Backend | Model runs | Setup | First audio | Notes |
|---|---|---|---|---|
| `embedded` | in the daemon | `./setup.sh` (~1.1 GB venv) | **0.2–0.8 s** | Fastest. Needs Python deps locally |
| `http` | in a container, or any OpenAI-compatible service | `/dq-local-tts:tts server up` (1.9 GB image) | **~2.6 s** | No Python deps here. Docker on macOS is CPU-only — no Metal — hence slower |
| `say` (default on macOS) | macOS's built-in `say` | nothing | **~0.5 s** | macOS only. Zero install; system voices instead of Kokoro's |

```
clean → split → [chunk 1] ─┐
                [chunk 2] ─┼─► backend ──► WAV bytes ──► played on this machine
                [chunk 3] ─┘
```

Chunks are requested one ahead of playback, so audio starts on chunk one on
every backend.

### What `/dq-local-tts:tts server up` does

It is idempotent and says which of four situations it found — already
serving, container stopped, image present, or nothing local at all. The last is
the slow case and announces itself first:

```
FIRST-TIME SETUP: no local image found.
Pulling ghcr.io/csabakecskemeti/kokoro-tts-server:latest -- this is a multi-GB
download and will take several minutes. It happens once; later starts are seconds.
```

A locally built image wins over the published one — if you built it, you meant
it.

**If the pull fails, it builds from source instead of giving up.** The whole
build context ships inside the plugin at `server/` (32 KB), so an offline
machine, a proxied network, or an unpublished architecture still gets a working
server. Building is slower than pulling — it installs torch and bakes the model,
a few minutes — but it needs no registry access at all. `/dq-local-tts:tts server build`
forces that path deliberately.

The server lives at [`server/`](server/) — Dockerfile, compose file, FastAPI
app and its tests. It is the source of truth for the image: CI in this
repository builds it and publishes to
`ghcr.io/csabakecskemeti/kokoro-tts-server`. It has no dependency on the plugin
and can be run on its own with `docker compose up -d`.

The plugin checks `/health` for `service` and `api_version`, so it can tell
"server is down" from "server speaks a contract I don't understand". A server
that isn't a `kokoro-tts-server` is allowed through and treated as a generic
OpenAI `/v1/audio/speech` endpoint.

The host port is **42821** — deliberately odd, since 8080 collides with almost
everything, and below the macOS ephemeral range (49152+) so it can't clash with
a transient bind. Override with `KOKORO_TTS_PORT`.

## Install

```
/plugin marketplace add devquasar-ai/skills
/plugin install dq-local-tts@devquasar
```

Or point at a local checkout with `/plugin marketplace add /path/to/this/repo`.

**On a Mac it works right away**: the default backend is macOS's built-in `say`, which needs
nothing installed. Better voices (Premium / Siri) can be downloaded in System Settings →
Accessibility → Spoken Content, then picked with `/dq-local-tts:tts voice NAME`.

**For Kokoro's voices** (any OS; the only option on Linux), use the container (`http`, the
default off macOS) or the embedded backend, both described below.

Plugin commands are namespaced: `/dq-local-tts:tts` and `/dq-local-tts:speak`.

### Container (no Python deps here)

Only Docker is required — the image is pulled if available, and built from the
bundled `server/` source if not.

```sh
/dq-local-tts:tts server up          # pull-or-start the container, wait for /health
/dq-local-tts:tts backend http       # switch the plugin over (restarts the daemon)
/dq-local-tts:tts status             # confirm reachable and API-compatible
```

### macOS `say` (nothing to install)

Uses the system voices that ship with every Mac. `say` renders each chunk to a
WAV, so filtering, chunking, `/dq-local-tts:tts stop` and playback behave exactly as on the
other backends.

```sh
/dq-local-tts:tts backend say
/dq-local-tts:tts voice              # lists macOS voices, English first
/dq-local-tts:tts voice Samantha     # "" or unset = the system default voice
```

On this backend `/dq-local-tts:tts voice` sets `say_voice`, leaving the Kokoro `voice`
untouched for when you switch back. Speed scales `say`'s rate from 175 wpm.
More natural voices (Premium / Siri) can be downloaded in System Settings →
Accessibility → Spoken Content.

### Embedded (lowest latency)

Needs `espeak-ng` (Kokoro's phonemizer) and Python 3.9+.

```sh
brew install espeak-ng          # or: sudo apt-get install espeak-ng
./setup.sh                      # venv + torch + model cache
/dq-local-tts:tts backend embedded
```

`setup.sh` builds `~/.local/share/local-tts/venv` (~1.1 GB) outside the plugin
directory, so a plugin update never discards it. It refuses to run without
`espeak-ng` rather than failing later.

## Use

Speaking is on by default; it starts working after the next reply.

| Command | Effect |
|---|---|
| `/dq-local-tts:tts` | Status: plugin version, voice, speed, backend, daemon |
| `/dq-local-tts:tts off` / `/dq-local-tts:tts on` | Stop / resume speaking replies |
| `/dq-local-tts:tts stop` | Cut off what is playing right now |
| `/dq-local-tts:tts voice` | List voices for the current backend |
| `/dq-local-tts:tts voice bm_george` | Switch voice (restarts the daemon); on `say`, a macOS voice |
| `/dq-local-tts:tts speed 1.15` | 0.5–2.0; ~1.15 is a good skim speed |
| `/dq-local-tts:tts backend` | Show the current backend and its settings |
| `/dq-local-tts:tts backend embedded\|http\|say` | Switch synthesis backend |
| `/dq-local-tts:tts server up\|down\|restart\|status\|logs\|rm` | Manage the TTS container |
| `/dq-local-tts:tts server pull` / `build` | Fetch the published image, or build the bundled source |
| `/dq-local-tts:tts restart` | Restart the daemon (picks up new plugin code) |
| `/dq-local-tts:tts start` / `shutdown` | Start and preload / stop the daemon |
| `/dq-local-tts:tts log` | Tail the daemon log |
| `/dq-local-tts:speak <text>` | Say something one-off |

`scripts/tts-ctl.sh` exposes the same surface from a shell and honours
`LOCAL_TTS_HOME` if you want the runtime somewhere other than
`~/.local/share/local-tts`.

## Updating

```
/plugin marketplace update devquasar       # or update it from /plugin
/reload-plugins                            # or restart Claude Code
/dq-local-tts:tts restart                     # the daemon keeps running old code
```

A running daemon is never touched by a plugin update. `/dq-local-tts:tts status`
shows the plugin version and flags a daemon started from another version as
`STALE`, with the restart that fixes it. New hooks only load on
`/reload-plugins` or a restart of Claude Code.

## Config

`~/.local/share/local-tts/config.json`:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Whether the hooks speak |
| `voice` | `af_heart` | 28 voices; `scripts/config.py voices` lists them |
| `speed` | `1.0` | Playback rate |
| `lang_code` | `a` | `a` American, `b` British — set automatically with the voice |
| `max_chars` | `0` | `0` speaks the whole reply; otherwise truncate at a sentence boundary |
| `backend` | `say` on macOS, `http` elsewhere | `embedded`, `http` or `say` |
| `say_voice` | `""` | macOS voice for the `say` backend; empty = system default |
| `server_url` | `http://localhost:42821` | Used by the `http` backend |
| `request_timeout` | `120` | Seconds to wait on the `http` backend |

## Notes

- Every piece of text Claude writes in a turn is spoken once, in order. Text
  written before a tool call is spoken as the tool starts (`PreToolUse`); the
  rest plays at `Stop`. Repeated hooks for the same text are deduped.
- Hooks fire before the transcript catches up, so reading it directly spoke
  the *previous* piece — one step behind. The final reply is therefore taken
  from the `Stop` payload's `last_assistant_message`, and on `PreToolUse` the
  worker waits (up to 2 s) until the tool call itself is in the transcript.
- Claude Code occasionally leaves an intermediate text block out of the
  transcript entirely; such text cannot be spoken. The final reply is always
  spoken, since it comes from the payload.
- `SessionEnd` stops whatever is still playing.
- The daemon **refuses to start** when it cannot synthesize — missing venv, or
  an unreachable/incompatible server. Accepting text and failing into an unread
  log is a worse failure than never starting, so `/dq-local-tts:tts status` names the cause.
- The daemon exits after three idle hours on any backend, and restarts on the
  next reply. On `embedded` that hands back the model's ~1 GB; on `http` and
  `say` there was never a model here to hand back.
- Playback uses `afplay`, falling back to `paplay`/`aplay`/`ffplay`.
- The container bakes in the model **and all 28 voice tensors**, and runs with
  `HF_HUB_OFFLINE=1`. Without the voices, a first request for a new voice
  silently downloaded from HuggingFace — which defeats the point of a local
  service and fails outright on an offline host.
- Switching backends or voices restarts the daemon: the pipeline is built per
  language at load time.

## Next

- An MCP surface over the server engine, so other clients get `speak`,
  `clean_text` and `split_text` as tools. MCP can't replace the Stop hook —
  tools are called *during* generation, but the final reply only exists after
  it, so the hook stays the trigger.
- An MLX backend for native Apple-silicon speed. It can't be containerized,
  for the same reason the container is CPU-only: no Metal in the Docker VM.

---

Free and open source, by [DevQuasar](https://devquasar.com) (Csaba Kecskemeti). MIT license.
