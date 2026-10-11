# dq-session-db

*DQ = [DevQuasar](https://devquasar.com).*

**Your Claude Code history, kept and searchable.** Find what was said or decided in any past
session, read around it, see which files and commits it touched, and get the exact command to
resume it. Optionally, keep a short summary card per session, written by a model you choose.

**Why:** Claude Code deletes session transcripts after `cleanupPeriodDays` (30 days by
default). session-db keeps a searchable index of every conversation **and a compressed copy of
every transcript**, so nothing is lost, and a deleted session can be put back and resumed.

## Install

```
/plugin marketplace add devquasar-ai/skills
/plugin install dq-session-db@devquasar
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

### With local models

Claude picks up the `sessions` skill on its own. Smaller local models (run through Claude Code
with a local endpoint) often don't: they grep transcripts, query the database by hand, or look
in the wrong place. Call the skill explicitly and it works the same:

```
/dq-session-db:sessions find the session about the billing refactor
```

## Keeping it tidy

Every "find the session about X" is itself a new session that mentions X, so quick lookups and
small tests pile up and crowd the results. session-db handles this three ways:

- **The current session is never in its own results.**
- **Cleanup candidates** are sessions quiet for a day that wrote no files and made no commits,
  and are small: either a lookup (used session-db, 6 or fewer of your messages) or tiny (3 or
  fewer of your messages and under ~3,000 characters in total). Thresholds are in
  `config.json` under `cleanup`.
- **You choose what happens** with `/dq-session-db:cleanup`:

```
/dq-session-db:cleanup                  list candidates and why; changes nothing
/dq-session-db:cleanup hide             hide them: kept in the archive, out of results
/dq-session-db:cleanup remove           delete them, after you confirm, with a backup first
/dq-session-db:cleanup unhide SESSION   bring a hidden session back
/dq-session-db:cleanup auto on|off      hide new candidates automatically (never removes)
```

`hide` and `remove` also take session names or ids instead of the candidate list. Removing backs
up the database and moves the session's compressed transcript to
`~/.local/share/session-db/backups/removed-<date>/`, and a removed session is never indexed
again. `sessions --all` and `search --all` include hidden sessions.

## Your history, kept

Every transcript gets a compressed copy in `~/.local/share/session-db/transcripts/`, kept up to date
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
for it) is your call. Turn them on from Claude Code with `/dq-session-db:cards`:

```
/dq-session-db:cards                       status: model, auto on/off, cards made, sessions due
/dq-session-db:cards haiku                 headless `claude -p` on your own Claude Code login
/dq-session-db:cards claude sonnet         ... with another Claude model
/dq-session-db:cards local URL MODEL [KEY_ENV]   any OpenAI-compatible endpoint: Ollama, vLLM,
                                        LM Studio, OpenAI, OpenRouter, ... (`openai` is an alias)
/dq-session-db:cards model NAME            change the model, keep the provider
/dq-session-db:cards url URL               change the endpoint
/dq-session-db:cards key-env VAR           read the API key from an environment variable
/dq-session-db:cards key KEY               store the API key in config.json (kept at 0600)
/dq-session-db:cards thinking on|off       thinking switch for vLLM/Qwen-style servers
/dq-session-db:cards on | off              automatic cards on/off
/dq-session-db:cards test                  one call to the model
/dq-session-db:cards now [SESSION]         make cards now: one session, or every due one
/dq-session-db:cards disable               no cards at all; existing cards are kept
```

Examples:

```
/dq-session-db:cards haiku                                       # simplest, uses your Claude plan
/dq-session-db:cards local http://localhost:11434/v1 qwen3:8b    # Ollama, fully local
/dq-session-db:cards local '${MY_LLM_URL}/v1' my-model MY_LLM_KEY  # ${VAR} expands at call time
```

The config lives next to the database (`~/.local/share/session-db/config.json`). From a shell, the
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

## Upgrading

Your data lives in `~/.local/share/session-db/` (or `$XDG_DATA_HOME/session-db`; set
`SESSION_DB_HOME` to put it anywhere else, e.g. a backed-up disk). That's outside the plugin, so
uninstalling or updating it never touches your data, and outside `~/.claude`, which belongs to
Claude Code: it cleans that folder up and a reset or reinstall may wipe it, which is exactly
what this archive has to survive.

Versions before 0.4 kept the archive in `~/.claude/session-db/`. The first run of 0.4 copies it
to the new place, verifies the copy, and renames the old folder to
`~/.claude/session-db.migrated` (delete it when you're happy). When a new version changes the database format, it upgrades it in
place on first use, after saving a backup next to it (`sessions.db.pre-vN.bak`). An older version
refuses to open a database written by a newer one, instead of damaging it. Run only one copy
of session-db at a time: two installs from different marketplaces would write to the same
archive.

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

Database: `~/.local/share/session-db/sessions.db` (override the folder with `SESSION_DB_HOME`, or the file with `SESSION_DB` / `--db`).

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
- **Rebuild from the archive.** Recreate the whole database from the compressed transcripts
  (only cards would need the model again): the safety net for any future format change.
- **Export / import** in a version-neutral format (sessions, messages, cards as JSON lines),
  for moving between machines or across major versions.
- **More cleanup rules:** an exclude list for folders that should not be indexed; dormant
  sessions ranking lower.
- **Multiple machines.** Rows already record their `host`, so archives can be merged later.
- **Local-only sessions.** Flag a session or project so its content is only shown to, or
  summarized by, a local model.

---

Free and open source, by [DevQuasar](https://devquasar.com) (Csaba Kecskemeti). MIT license.
