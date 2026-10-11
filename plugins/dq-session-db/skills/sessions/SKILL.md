---
name: sessions
description: Search, read and resume past Claude Code sessions from a local full-text archive. Use when the user refers to another or earlier session (by name, id, or topic), asks what was said, decided or done before ("what did we decide about X", "where did I leave off on Y", "find the session where..."), wants to resume or continue earlier work, or when you need context from past work on a project before advising on it.
allowed-tools:
  - Bash
---

# /sessions — search and resume past sessions

Every Claude Code session on this machine is indexed into a SQLite full-text archive at
`~/.local/share/session-db/sessions.db` (outside Claude Code's own folder). The archive keeps its own copy of the conversation text, so
sessions stay searchable after Claude Code deletes old transcripts (`cleanupPeriodDays`, 30 by
default). Hooks index new messages at session start and end, and every command catches up
before it answers.

Run everything through:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sessiondb.py" <command> ...
```

## Commands

| Command | Use |
|---|---|
| `search "<words>"` | Best-matching messages, with a snippet, date, session and message `#id`. |
| `search "<words>" --by-session` | One line per matching session. Good first step for "which session was that?". |
| `search "<words>" --cards` | Search the summary cards (sessions that have one). |
| `sessions [-p project] [--since 7d] [--status]` | Recent sessions with name, date, project, size; `--status` adds each card's status line. |
| `card <session>` | Summary card: goal, status, timeline, decisions, outcomes, open threads, key facts (each citing a message `#id`), plus files written and git commits taken from tool calls. Files and git show even with cards off. |
| `card <session> --refresh` | Bring the card up to date first (only if cards are set up). |
| `show <session> --around <#id> [-C 6]` | Read the conversation around a search hit or a card citation. |
| `show <session> [--last 12 \| --head 10 \| --all] [--role user] [--full]` | Read a session's messages. |
| `resume <session>` | The exact command to resume it: `cd <dir> && claude --resume <id>`. |
| `restore <session>` | Put a transcript Claude Code deleted back from the compressed archive, so it can be resumed. |
| `reindex [--full]` | Catch up now; `--full` re-reads every transcript. |
| `stats` / `config` | Archive summary / whether cards are on and which model makes them. |

`<session>` can be a full id, an id prefix (`8d779eeb`), or a session name or title
(e.g. `billing-refactor`).

Search filters: `-p/--project <substr>`, `-s/--session <session>`, `--role user|assistant|summary`,
`--since 7d|4w|3m|2026-09-01`, `--recent` (newest first), `-n` (limit). Plain words are ANDed;
use `--raw` for FTS5 syntax (`"exact phrase"`, `OR`, `NEAR`, `prefix*`).

## Summary cards are opt-in

Cards are off until the user turns them on; everything else works without them. Never change
the card setup yourself: the user picks the model and pays for it. If they ask how, point them
to `/dq-session-db:cards` (no arguments shows the status), e.g. `/dq-session-db:cards haiku`,
`/dq-session-db:cards local URL MODEL [KEY_ENV]`, `/dq-session-db:cards on|off`.

Once on, cards update in the background when a session has been quiet for 30 minutes and has
grown, and a card is only ever extended with new messages, never re-read from scratch.

## How to answer from it

1. For "where are we on X" questions, start with `sessions -p X --status` and `card`. For
   "what did we decide / say about Y", start with `search`.
2. Search. If the first query misses, try different words: the archive holds what was actually
   said, which is often not the word the user uses now.
3. Read around the best hits with `show --around` before concluding. A snippet is not enough
   to report a decision, and a card item is a pointer: check its `#id` before quoting it.
4. Answer with the source: session name and id, date, and what was said. Quote the user's own
   words when they matter.
5. If the user wants to continue that work, give them the `resume` command. Resuming only works
   from the directory the session started in, which is why it includes the `cd`. If the
   transcript was deleted (`stats` counts these), run `restore <session>` first.

What is indexed: user and assistant text of main-thread messages, plus compaction summaries;
from tool calls, only the files written and git commits/pushes. Tool output and subagent
transcripts are not indexed, but every transcript is kept whole as a compressed copy.
