# session-db

**Your Claude Code history, kept and searchable.** Find what was said or decided in any past
session, read around it, see which files and commits it touched, and get the exact command to
resume it. Optionally, keep a short summary card per session, written by a model you choose.

**Why:** Claude Code deletes session transcripts after `cleanupPeriodDays` (30 days by
default). session-db keeps a searchable index of every conversation **and a compressed copy of
every transcript**, so nothing is lost, and a deleted session can be put back and resumed.

## Install

```
/plugin marketplace add devquasar-ai/skills
/plugin install session-db@devquasar
```

That's all. The first command you run (or the next session start) builds the archive, and
later runs only read what is new. Requires Python 3.9+ with SQLite FTS5 (macOS and most Linux
builds have it). No packages to install.

To also import an older backup copy of `~/.claude/projects`:

```bash
sessiondb.py index /path/to/backup/projects ~/.claude/projects
```

## Use

Just ask Claude, in any session. The `sessions` skill picks up questions like:

- "what did we decide about the database migration?"
- "find the session where I set up the MCP server"
- "where did I leave off on the billing service?"

Or run it yourself (`sessiondb.py` is in the plugin's `scripts/` folder):

```bash
sessiondb.py search "migration rollback" --by-session   # which sessions talked about it
sessiondb.py show 3f2a91c0 --around 1234 -C 6          # read around a search hit
sessiondb.py sessions -p billing --status              # recent sessions in a project
sessiondb.py card billing-refactor                     # summary + files + commits
sessiondb.py resume billing-refactor
#   cd ~/code/billing && claude --resume 3f2a91c0-...
sessiondb.py restore billing-refactor                  # bring back a deleted transcript
```

A session can be named by its id, the start of its id, or its name (`/rename`) or title.

## Your history, kept

Every transcript gets a compressed copy in `~/.claude/session-db/transcripts/`, kept up to date
as the session grows. If Claude Code has deleted the original, `restore <session>` writes it
back byte-for-byte, and `claude --resume` works again. `stats` shows how many sessions now exist
only in the archive.

Size: transcripts compress to roughly half. Screenshots are stored inside transcripts as
images, and those barely compress, so image-heavy sessions dominate the archive.

## Summary cards (opt-in)

A card is a short summary of one session: title, goal, status, a timeline, decisions,
outcomes, open threads and key facts. Every item cites the message `#id` it came from, so it
can be checked with `show --around`. Cards are searchable too (`search --cards`).

**Cards are off by default.** Summarizing needs a language model, and which one (and who pays
for it) is your call. Turn them on from Claude Code with `/session-db:cards`:

```
/session-db:cards                       status: model, auto on/off, cards made, sessions due
/session-db:cards haiku                 headless `claude -p` on your own Claude Code login
/session-db:cards claude sonnet         ... with another Claude model
/session-db:cards local URL MODEL [KEY_ENV]   any OpenAI-compatible endpoint: Ollama, vLLM,
                                        LM Studio, OpenAI, OpenRouter, ... (`openai` is an alias)
/session-db:cards model NAME            change the model, keep the provider
/session-db:cards url URL               change the endpoint
/session-db:cards key-env VAR           read the API key from an environment variable
/session-db:cards key KEY               store the API key in config.json (kept at 0600)
/session-db:cards thinking on|off       thinking switch for vLLM/Qwen-style servers
/session-db:cards on | off              automatic cards on/off
/session-db:cards test                  one call to the model
/session-db:cards now [SESSION]         make cards now: one session, or every due one
/session-db:cards disable               no cards at all; existing cards are kept
```

Examples:

```
/session-db:cards haiku                                       # simplest, uses your Claude plan
/session-db:cards local http://localhost:11434/v1 qwen3:8b    # Ollama, fully local
/session-db:cards local '${MY_LLM_URL}/v1' my-model MY_LLM_KEY  # ${VAR} expands at call time
```

The config lives next to the database (`~/.claude/session-db/config.json`). From a shell, the
same verbs work as `scripts/cards-ctl.sh <verb>`.

**When cards are made.** In the background, from the session hooks, for a session that has
been quiet for 30 minutes and has at least 6 messages (first card) or 20 new ones (update). At
most 5 sessions per run. `card <session> --refresh` updates one on demand. All thresholds are
in `config.json` under `cards`.

**How, and what it costs.** Two levels, because rewriting one running card batch by batch
drifts toward the latest batch and forgets how long sessions started:

1. Messages are cut into stretches of ~60k characters (each message capped at 3,000), and each
   stretch is summarized on its own. Stretch summaries are stored, so each message is
   summarized once, ever.
2. The card is written in one pass over all stretch summaries, in order: a few thousand tokens
   even for a very long session.

Only conversation text is sent, never tool output. As a reference, 81 sessions of real use came
to about 400k tokens in total, the largest single session about 91k, and a busy day adds
5–40k. With `claude -p`, Claude Code's own system prompt is replaced and tools are disabled, so
the overhead per call is a few hundred tokens.

## Privacy

- Everything stays on your machine. The database and transcript copies are created readable
  only by you.
- Nothing is sent anywhere unless you turn cards on, and then only to the model you chose.
- Nothing is redacted: a password or key that appeared in a session is in the archive too.
  That's deliberate (it may be the only copy), and it's why the files are private.

## How it works

- **Source:** `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`, one JSON event per line.
- **Indexed for search:** user and assistant text from the main thread, plus compaction
  summaries. System reminders and command wrappers are stripped. From tool calls, the files
  written (Write/Edit) and git commits/pushes are recorded. Tool output and subagent transcripts
  are not indexed (they are still in the compressed copy).
- **Always current:** every read command first reads whatever is new (a no-change run only
  stats each file). `SessionStart`/`SessionEnd` hooks also run it in the background, which
  rescues transcripts Claude Code is about to delete at startup, and then update due cards.
- **Incremental:** transcripts are append-only, so each file is read from the byte offset the
  last run stopped at, and the compressed copy grows by appending (concatenated gzip members are
  one valid gzip stream). Messages are keyed by event `uuid`, so re-reading never duplicates.
- **Resume:** records the directory each session started in, because `claude --resume` only
  finds a session from there.

Database: `~/.claude/session-db/sessions.db` (override with `SESSION_DB` or `--db`).

| Table | Holds |
|---|---|
| `sessions` | id, name, title, start dir, project, branch, first/last dates, counts, host |
| `messages` | one row per message: uuid, session, timestamp, role, text; FTS5-indexed |
| `touches` | files written and git operations per session |
| `segments` | stretch summaries (cards only) |
| `cards` | one card per session and the last message it covers; FTS5-indexed |
| `files` | per transcript: byte offset indexed, bytes copied to the archive |

## Roadmap

- **Continue a restored session from a summary.** Resuming a long restored transcript
  replays all of it; an option to continue from its card or a condensed summary instead.
- **Safe storage beyond this machine.** The archive is local today; an optional persistent
  layer (another disk, a NAS, a private remote) so history survives the machine too.
- **Search by meaning** (vector search), opt-in, next to the keyword index, for when the words
  you use now differ from the words used then. Embeddings from an endpoint you choose.
- **Retiring sessions.** Suggest, never delete: mark old sessions dormant so they rank lower; an
  exclude list for folders that should not be indexed; a manual `forget <session>`.
- **Multiple machines.** Rows already record their `host`, so archives can be merged later.
- **Local-only sessions.** Flag a session or project so its content is only shown to, or
  summarized by, a local model.

---

Free and open source, by [DevQuasar](https://devquasar.com) (Csaba Kecskemeti). MIT license.
