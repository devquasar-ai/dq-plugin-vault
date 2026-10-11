#!/usr/bin/env python3
"""Searchable archive of Claude Code sessions.

Claude Code keeps session transcripts as JSONL under ~/.claude/projects and
deletes them after `cleanupPeriodDays` (30 by default). This indexes the
conversational text of every session into a SQLite FTS5 database that keeps
its own copy, so history stays searchable after the transcripts are gone.

Standard library only. Indexing is incremental: transcripts are append-only,
so each file is read from the byte offset where the last run stopped.
"""

import argparse
import fcntl
import gzip
import json
import os
import re
import socket
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cards  # noqa: E402

HOME = os.path.expanduser("~")
DEFAULT_SOURCE = os.environ.get("SESSION_DB_SOURCE", os.path.join(HOME, ".claude", "projects"))
DEFAULT_DB = os.environ.get(
    "SESSION_DB", os.path.join(HOME, ".claude", "session-db", "sessions.db")
)
MAX_TEXT = 20000  # per message; long pastes are truncated, not dropped
SCHEMA_VERSION = 3

# Harness-injected text that is noise for search.
NOISE_BLOCK = re.compile(
    r"<(system-reminder|task-notification|local-command-stdout|local-command-stderr)>.*?</\1>",
    re.S,
)
COMMAND_TAG = re.compile(r"</?(command-name|command-message|command-args)>")


# --------------------------------------------------------------------------- db

def connect(path):
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    new = not os.path.exists(path)
    db = sqlite3.connect(path, timeout=30)
    if new:
        os.chmod(path, 0o600)  # the archive holds whatever was said, secrets included
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY, session_id TEXT, size INTEGER, offset INTEGER,
            mtime REAL, indexed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            name TEXT, ai_title TEXT, cwd TEXT, project TEXT, git_branch TEXT,
            started TEXT, updated TEXT,
            n_user INTEGER DEFAULT 0, n_assistant INTEGER DEFAULT 0,
            first_prompt TEXT, last_prompt TEXT, host TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            uuid TEXT UNIQUE, session_id TEXT NOT NULL,
            ts TEXT, role TEXT, text TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, ts);
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            text, content='messages', content_rowid='id', tokenize='porter unicode61'
        );
        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
        END;
        -- What a session did, read from its tool calls: files written, git operations.
        CREATE TABLE IF NOT EXISTS touches (
            session_id TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
            first_ts TEXT, last_ts TEXT,
            PRIMARY KEY (session_id, kind, value)
        );
        CREATE TABLE IF NOT EXISTS cards (
            session_id TEXT PRIMARY KEY, card TEXT, text TEXT,
            covers_through INTEGER, n_messages INTEGER,
            generated_at TEXT, model TEXT
        );
        CREATE TABLE IF NOT EXISTS segments (
            session_id TEXT NOT NULL, seg_no INTEGER NOT NULL,
            first_id INTEGER, last_id INTEGER, summary TEXT,
            PRIMARY KEY (session_id, seg_no)
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
            session_id UNINDEXED, text, tokenize='porter unicode61'
        );
        """
    )
    cols = {r[1] for r in db.execute("PRAGMA table_info(files)")}
    if "archived" not in cols:
        # v3: compressed copies of the raw transcripts; existing rows start at 0 and are
        # backfilled from whatever source files still exist.
        db.execute("ALTER TABLE files ADD COLUMN archived INTEGER DEFAULT 0")
    row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row and int(row[0]) < 2:
        # v2 reads tool calls too: re-read every transcript. Stored messages are
        # kept (uuid dedupe), and files that are gone just keep what we have.
        db.execute("DELETE FROM files")
    db.execute(
        "INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
    )
    db.commit()
    return db


# ---------------------------------------------------------------------- parsing

def clean(text):
    text = NOISE_BLOCK.sub("", text)
    text = COMMAND_TAG.sub("", text)
    return text.strip()[:MAX_TEXT]


def message_text(event):
    """Human-readable text of a user/assistant event, or '' if it has none."""
    content = (event.get("message") or {}).get("content")
    if isinstance(content, str):
        return clean(content)
    if isinstance(content, list):
        # Only text blocks: tool calls and tool results are mechanics, not conversation.
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return clean("\n".join(p for p in parts if p))
    return ""


GIT_OP = re.compile(r"\b(git\s+(push|commit|clone|remote\s+add|init)|gh\s+(repo|pr)\s+\w+)\b")


def record_touches(db, sid, event):
    content = (event.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    ts = event.get("timestamp")
    for b in content:
        if not isinstance(b, dict) or b.get("type") != "tool_use":
            continue
        inp = b.get("input") or {}
        name = b.get("name")
        if name in ("Write", "Edit", "MultiEdit", "NotebookEdit") and inp.get("file_path"):
            touch(db, sid, "file", inp["file_path"], ts)
        elif name == "Bash" and GIT_OP.search(inp.get("command") or ""):
            touch(db, sid, "git", git_summary(inp["command"], event.get("cwd") or ""), ts)


def git_summary(cmd, cwd):
    """'~/repo: commit, push — <subject>' from a shell command that ran git."""
    m = re.search(r"\bcd\s+([^\s;&|]+)", cmd)
    repo = os.path.expanduser(m.group(1)) if m else cwd
    ops = []
    for g in GIT_OP.finditer(cmd):
        op = " ".join(g.group(1).split()[:2]) if g.group(1).startswith("gh") else g.group(2).split()[0]
        if op not in ops:
            ops.append(op)
    subject = ""
    m = re.search(r"""\s-[a-zA-Z]*m\s+(["'])(.+?)\1""", cmd, re.S) or re.search(
        r"-F\s+-\s+<<-?\s*'?(\w+)'?[^\n]*\n(.+?)\n", cmd)
    if m:
        subject = m.group(2).strip().splitlines()[0][:100]
    home = os.path.expanduser("~")
    out = f"{repo.replace(home, '~', 1)}: {', '.join(ops)}"
    return out + (f" — {subject}" if subject else "")


def touch(db, sid, kind, value, ts):
    db.execute(
        """INSERT INTO touches(session_id, kind, value, first_ts, last_ts) VALUES (?,?,?,?,?)
           ON CONFLICT(session_id, kind, value) DO UPDATE SET
             first_ts = min(coalesce(first_ts, excluded.first_ts), excluded.first_ts),
             last_ts = max(coalesce(last_ts, ''), excluded.last_ts)""",
        (sid, kind, value, ts, ts),
    )


def archive_path(db_path, src):
    """Where the compressed copy of a transcript lives: mirrors ~/.claude/projects layout."""
    return os.path.join(os.path.dirname(db_path), "transcripts",
                        os.path.basename(os.path.dirname(src)), os.path.basename(src) + ".gz")


def archive_file(db, db_path, path, upto):
    """Bring the compressed copy of `path` up to byte `upto` (complete lines only).

    New bytes are appended as an extra gzip member; concatenated members are a valid gzip
    stream, so the copy grows incrementally like the transcript does.
    """
    row = db.execute("SELECT archived FROM files WHERE path=?", (path,)).fetchone()
    done = (row["archived"] or 0) if row else 0
    dest = archive_path(db_path, path)
    if done > upto or not os.path.exists(dest):
        done = 0  # rewritten transcript or missing copy: start the copy over
    if done == upto:
        return
    os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
    with open(path, "rb") as f:
        f.seek(done)
        chunk = f.read(upto - done)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | (os.O_APPEND if done else os.O_TRUNC), 0o600)
    with os.fdopen(fd, "ab" if done else "wb") as out:
        out.write(gzip.compress(chunk))
    db.execute("UPDATE files SET archived=? WHERE path=?", (upto, path))


def index_file(db, path, host, force=False, db_path=None):
    """Index new lines of one transcript. Returns the number of messages added."""
    try:
        st = os.stat(path)
    except OSError:
        return 0
    row = db.execute("SELECT size, offset FROM files WHERE path=?", (path,)).fetchone()
    offset = 0
    if row and not force:
        if st.st_size == row["size"]:
            if db_path:
                archive_file(db, db_path, path, row["offset"])  # backfill copies
            return 0
        if st.st_size > row["offset"]:
            offset = row["offset"]
        else:
            db.execute("UPDATE files SET archived=0 WHERE path=?", (path,))
        # A file that shrank was rewritten: read it again from the start. Messages
        # already stored are kept (uuid dedupe), so nothing is lost either way.

    sid = os.path.splitext(os.path.basename(path))[0]
    sess = {}
    added = 0
    with open(path, "rb") as f:
        f.seek(offset)
        while True:
            line = f.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                break  # partial line still being written; pick it up next run
            offset += len(line)
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            sid = ev.get("sessionId") or sid
            t = ev.get("type")
            if t == "custom-title" and ev.get("customTitle"):
                sess["name"] = ev["customTitle"]
            elif t == "ai-title" and ev.get("aiTitle"):
                sess["ai_title"] = ev["aiTitle"]
            if ev.get("cwd") and "cwd" not in sess:
                sess["cwd"] = ev["cwd"]
                sess["project"] = os.path.basename(ev["cwd"].rstrip("/")) or ev["cwd"]
            if ev.get("gitBranch"):
                sess["git_branch"] = ev["gitBranch"]
            ts = ev.get("timestamp")
            if ts:
                sess.setdefault("started", ts)
                sess["updated"] = ts
            if t not in ("user", "assistant") or ev.get("isSidechain") or ev.get("isMeta"):
                continue
            if t == "assistant":
                record_touches(db, sid, ev)  # idempotent, so re-reads are safe
            text = message_text(ev)
            if not text:
                continue
            role = t
            if t == "user" and ev.get("isCompactSummary"):
                role = "summary"
            cur = db.execute(
                "INSERT OR IGNORE INTO messages(uuid, session_id, ts, role, text) VALUES (?,?,?,?,?)",
                (ev.get("uuid") or f"{sid}:{offset}", sid, ts, role, text),
            )
            if cur.rowcount:
                added += 1
                key = "n_user" if role == "user" else "n_assistant" if role == "assistant" else None
                if key:
                    sess[key] = sess.get(key, 0) + 1
                if role == "user":
                    sess.setdefault("first_prompt", text[:500])
                    sess["last_prompt"] = text[:500]

    upsert_session(db, sid, sess, host)
    db.execute(
        """INSERT INTO files(path, session_id, size, offset, mtime, indexed_at) VALUES (?,?,?,?,?,?)
           ON CONFLICT(path) DO UPDATE SET session_id=excluded.session_id, size=excluded.size,
             offset=excluded.offset, mtime=excluded.mtime, indexed_at=excluded.indexed_at""",
        (path, sid, st.st_size, offset, st.st_mtime, now_iso()),
    )
    if db_path:
        archive_file(db, db_path, path, offset)
    return added


def upsert_session(db, sid, s, host):
    db.execute("INSERT OR IGNORE INTO sessions(session_id, host) VALUES (?,?)", (sid, host))
    cur = db.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    merged = dict(cur)
    for k in ("name", "ai_title", "git_branch", "last_prompt"):
        if s.get(k):
            merged[k] = s[k]
    # `claude --resume` only finds a session from the directory it was started in,
    # so keep the first cwd rather than wherever the session later cd'd to.
    for k in ("cwd", "project"):
        if s.get(k) and not merged[k]:
            merged[k] = s[k]
    if s.get("first_prompt") and not merged["first_prompt"]:
        merged["first_prompt"] = s["first_prompt"]
    if s.get("started") and (not merged["started"] or s["started"] < merged["started"]):
        merged["started"] = s["started"]
    if s.get("updated") and (not merged["updated"] or s["updated"] > merged["updated"]):
        merged["updated"] = s["updated"]
    merged["n_user"] = (merged["n_user"] or 0) + s.get("n_user", 0)
    merged["n_assistant"] = (merged["n_assistant"] or 0) + s.get("n_assistant", 0)
    cols = [k for k in merged if k != "session_id"]
    db.execute(
        f"UPDATE sessions SET {', '.join(c + '=?' for c in cols)} WHERE session_id=?",
        [merged[c] for c in cols] + [sid],
    )


def transcripts(root):
    for dirpath, dirnames, filenames in os.walk(root):
        # Subagent transcripts are sidechains of their parent; memory is not a session.
        dirnames[:] = [d for d in dirnames if d not in ("subagents", "memory", ".git")]
        for fn in filenames:
            if fn.endswith(".jsonl"):
                yield os.path.join(dirpath, fn)


# --------------------------------------------------------------------- commands

def run_index(db, db_path, roots=None, force=False, wait=False):
    """Index new transcript lines. Returns (files, added) or None if another run holds the lock."""
    lock = open(db_path + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except OSError:
        return None
    host = socket.gethostname().split(".")[0]
    files = added = 0
    try:
        for root in roots or [DEFAULT_SOURCE]:
            for p in transcripts(os.path.expanduser(root)):
                added += index_file(db, p, host, force=force, db_path=db_path)
                db.commit()
                files += 1
    finally:
        lock.close()
    return files, added


def cmd_index(db, args):
    t0 = time.time()
    res = run_index(db, args.db, args.path, force=args.force)
    if res is None:
        if not args.quiet:
            print("Another indexer is running; skipping.")
        return
    files, added = res
    if getattr(args, "cards", False):
        cmd_cards(db, argparse.Namespace(db=args.db, auto=True, all=False, full=False,
                                         session=None, quiet=args.quiet))
    if not args.quiet:
        total = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        print(f"Scanned {files} transcripts, added {added} messages in {time.time()-t0:.1f}s. "
              f"{total} sessions indexed.")


def fts_query(q, raw):
    if raw or re.search(r'["*()]|\b(AND|OR|NOT|NEAR)\b', q):
        return q
    words = re.findall(r"\w+", q)
    return " ".join(f'"{w}"' for w in words)


def since_ts(s):
    if not s:
        return None
    m = re.fullmatch(r"(\d+)([dwm])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = n * {"d": 1, "w": 7, "m": 30}[unit]
        return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    return s  # an ISO date like 2026-09-01


def cmd_search(db, args):
    q = fts_query(" ".join(args.query), args.raw)
    if args.cards:
        return search_cards(db, q, args)
    where, params = ["messages_fts MATCH ?"], [q]
    if args.project:
        where.append("(s.project LIKE ? OR s.cwd LIKE ?)")
        params += [f"%{args.project}%"] * 2
    if args.session:
        sid = resolve(db, args.session)
        if not sid:
            return
        where.append("m.session_id = ?")
        params.append(sid)
    if args.role:
        where.append("m.role = ?")
        params.append(args.role)
    if args.since:
        where.append("m.ts >= ?")
        params.append(since_ts(args.since))
    sql = f"""
        SELECT m.id, m.ts, m.role, m.session_id, s.name, s.ai_title, s.project,
               snippet(messages_fts, 0, '[', ']', ' … ', 24) AS snip
        FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid
        JOIN sessions s ON s.session_id = m.session_id
        WHERE {' AND '.join(where)}
        ORDER BY {'m.ts DESC' if args.recent else 'bm25(messages_fts)'}
        LIMIT ?"""
    try:
        rows = db.execute(sql, params + [args.limit]).fetchall()
    except sqlite3.OperationalError as e:
        sys.exit(f"Bad query {q!r}: {e}. Use --raw for FTS5 syntax, or plain words.")
    if not rows:
        print("No matches.")
        return
    if args.by_session:
        seen = {}
        for r in rows:
            seen.setdefault(r["session_id"], []).append(r)
        for sid, hits in seen.items():
            r = hits[0]
            print(f"{sid[:8]}  {label(r)}  [{r['project'] or '?'}]  {len(hits)} hit(s), latest {day(max(h['ts'] or '' for h in hits))}")
        return
    for r in rows:
        snip = " ".join(r["snip"].split())
        print(f"#{r['id']}  {day(r['ts'])}  {r['session_id'][:8]}  {label(r)}  [{r['project'] or '?'}]  {r['role']}")
        print(f"    {snip}")
    print("\nRead around a hit: sessiondb.py show <session> --around <#id>")


def search_cards(db, q, args):
    try:
        rows = db.execute(
            """SELECT f.session_id, s.name, s.ai_title, s.project, s.updated, c.card,
                      snippet(cards_fts, 1, '[', ']', ' … ', 24) AS snip
               FROM cards_fts f JOIN sessions s ON s.session_id = f.session_id
               JOIN cards c ON c.session_id = f.session_id
               WHERE cards_fts MATCH ? ORDER BY bm25(cards_fts) LIMIT ?""",
            (q, args.limit)).fetchall()
    except sqlite3.OperationalError as e:
        sys.exit(f"Bad query {q!r}: {e}")
    if not rows:
        print("No card matches (sessions without a card are not covered; try without --cards).")
        return
    for r in rows:
        c = json.loads(r["card"])
        print(f"{r['session_id'][:8]}  {day(r['updated'])}  {c.get('title') or label(r)}  [{r['project'] or '?'}]")
        print(f"    status: {c.get('status', '')}")
        print(f"    match:  {' '.join(r['snip'].split())}")
    print("\nFull card: sessiondb.py card <session>")


def cmd_sessions(db, args):
    where, params = [], []
    if args.project:
        where.append("(project LIKE ? OR cwd LIKE ?)")
        params += [f"%{args.project}%"] * 2
    if args.since:
        where.append("updated >= ?")
        params.append(since_ts(args.since))
    sql = "SELECT * FROM sessions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated DESC LIMIT ?"
    for r in db.execute(sql, params + [args.limit]):
        n = (r["n_user"] or 0) + (r["n_assistant"] or 0)
        print(f"{r['session_id'][:8]}  {day(r['updated'])}  {label(r):40.40}  [{r['project'] or '?'}]  {n} msgs")
        c = db.execute("SELECT card FROM cards WHERE session_id=?", (r["session_id"],)).fetchone()
        if c and args.status:
            print(f"          {json.loads(c[0]).get('status', '')}")


def cmd_show(db, args):
    sid = resolve(db, args.session)
    if not sid:
        return
    s = db.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    print(f"== {label(s)}  ({sid})")
    print(f"   project: {s['cwd'] or '?'}  branch: {s['git_branch'] or '-'}  host: {s['host']}")
    print(f"   {day(s['started'])} → {day(s['updated'])}   user {s['n_user']} / assistant {s['n_assistant']}")
    print(f"   resume: {resume_cmd(s)}\n")
    if args.around:
        mid = int(str(args.around).lstrip("#"))
        rows = db.execute(
            """SELECT * FROM (SELECT * FROM messages WHERE session_id=? AND id<=? ORDER BY id DESC LIMIT ?)
               UNION SELECT * FROM (SELECT * FROM messages WHERE session_id=? AND id>? ORDER BY id LIMIT ?)
               ORDER BY id""",
            (sid, mid, args.context + 1, sid, mid, args.context),
        ).fetchall()
    elif args.head:
        rows = db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY id LIMIT ?",
                          (sid, args.head)).fetchall()
    elif args.role == "user" and args.all:
        rows = db.execute("SELECT * FROM messages WHERE session_id=? AND role='user' ORDER BY id",
                          (sid,)).fetchall()
    elif args.all:
        rows = db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY id", (sid,)).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM (SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?) ORDER BY id",
            (sid, args.last),
        ).fetchall()
    for m in rows:
        if args.role and m["role"] != args.role:
            continue
        mark = " <<" if args.around and m["id"] == int(str(args.around).lstrip("#")) else ""
        text = m["text"] if args.full else trunc(m["text"], args.width)
        print(f"#{m['id']} [{(m['ts'] or '')[:16]}] {m['role'].upper()}{mark}:\n{text}\n")


def cmd_card(db, args):
    sid = resolve(db, args.session)
    if not sid:
        return
    if args.refresh or args.full:
        cfg = cards.load_config(args.db)
        if not cfg or not cfg["summarizer"]:
            print(cards.config_status(args.db, cfg))
            return
        try:
            cards.build_card(db, sid, cfg, full=args.full)
        except (urllib_errors() + (RuntimeError,)) as e:
            print(f"Card generation failed: {e}", file=sys.stderr)
    print(cards.render(db, sid))


def cmd_cards(db, args):
    cfg = cards.load_config(args.db)
    if not cfg or not cfg["summarizer"]:
        if not args.quiet:
            print(cards.config_status(args.db, cfg))
        return
    if args.auto and not cfg["cards"]["auto"]:
        return
    lock = open(args.db + ".cards.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if not args.quiet:
            print("Cards are already being generated; skipping.")
        return
    if args.session:
        todo = [resolve(db, args.session)]
    elif args.all:
        todo = [r[0] for r in db.execute(
            """SELECT session_id FROM sessions WHERE
               (SELECT COUNT(*) FROM messages m WHERE m.session_id = sessions.session_id) >= ?
               ORDER BY updated DESC""", (cfg["cards"]["min_messages"],))]
    else:
        todo = cards.due_sessions(db, cfg["cards"])[: cfg["cards"]["max_per_run"]]
    log = (lambda *a: None) if args.quiet else print
    done = 0
    for sid in filter(None, todo):
        try:
            if cards.build_card(db, sid, cfg, full=args.full, log=log):
                done += 1
        except (urllib_errors() + (RuntimeError,)) as e:
            log(f"  {sid[:8]}: failed: {e}")
            if isinstance(e, urllib_errors()):
                break  # endpoint unreachable: stop, the next run retries
    log(f"{done} card(s) updated.")


def cmd_config(db, args):
    print(cards.config_status(args.db, cards.load_config(args.db)))


def cmd_cards_setup(db, args):
    if args.provider == "off":
        cfg_path = cards.config_path(args.db)
        if os.path.exists(cfg_path):
            with open(cfg_path) as f:
                summ = json.load(f).get("summarizer") or {}
            cards.write_config(args.db, {**summ, "enabled": False})
        print("Session cards are off. Existing cards are kept and still shown.")
        return
    if args.provider == "claude":
        summ = {"provider": "claude", "model": args.model or "haiku"}
    else:
        if not args.base_url or not args.model:
            sys.exit("openai needs --base-url and --model")
        summ = {"provider": "openai", "base_url": args.base_url, "model": args.model}
        if args.key_env:
            summ["api_key_env"] = args.key_env
        if args.no_thinking:  # vLLM/Qwen-style switch; harmless elsewhere
            summ["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    path = cards.write_config(args.db, summ)
    cfg = cards.load_config(args.db)
    print(f"Wrote {path}\n")
    print(cards.config_status(args.db, cfg))
    if not args.no_test:
        print("\nTesting the summarizer ...", end=" ", flush=True)
        try:
            out, model = cards.call_llm(cfg["summarizer"], "Output JSON only.", 'Return {"ok": true}')
            cards.parse_card(out)
            print(f"ok ({model})")
        except Exception as e:  # report any failure; the config stays so it can be fixed
            print(f"FAILED: {e}")


CTL_USAGE = """usage: cards-ctl <verb>
  status                         what makes cards, auto on/off, how many cards exist
  on | off                       automatic cards on/off (manual `card --refresh` still works)
  haiku | claude [MODEL]         use headless `claude -p` on your Claude Code login
  local URL MODEL [KEY_ENV]      use an OpenAI-compatible endpoint (local or any provider)
  openai URL MODEL [KEY_ENV]     same as local
  model NAME                     change the model, keep the provider
  url URL                        change the endpoint URL
  key-env VAR                    read the API key from environment variable VAR
  key KEY                        store the API key in config.json (file is 0600)
  thinking on|off                vLLM/Qwen thinking switch for endpoint providers
  test                           one call to the summarizer
  now [SESSION]                  make/update cards now: SESSION, or every due session
  disable                        no cards at all (existing cards are kept)"""


def cmd_cards_ctl(db, args):
    v = args.verb or ["status"]
    verb, rest = v[0], v[1:]
    raw = cards.read_raw(args.db)
    summ = dict(raw.get("summarizer") or {})
    say = print

    def save_summ(msg):
        raw["summarizer"] = summ
        cards.write_raw(args.db, raw)
        say(msg)

    if verb == "status":
        say(cards.config_status(args.db, cards.load_config(args.db)))
        n = db.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        total = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        cfg = cards.load_config(args.db)
        due = len(cards.due_sessions(db, cfg["cards"])) if cfg else 0
        say(f"cards: {n} of {total} sessions" + (f", {due} due now" if cfg else ""))
    elif verb in ("on", "off"):
        if verb == "on" and not summ:
            sys.exit("No summarizer yet. Pick one first: `haiku`, `claude MODEL`, or `local URL MODEL`.")
        raw.setdefault("cards", dict(cards.DEFAULT_CARDS))["auto"] = verb == "on"
        summ["enabled"] = True
        save_summ(f"auto cards {verb}")
    elif verb in ("haiku", "claude"):
        model = "haiku" if verb == "haiku" else (rest[0] if rest else "haiku")
        summ = {"provider": "claude", "model": model}
        save_summ(f"summarizer: claude -p --model {model} (your Claude Code login)")
    elif verb in ("local", "openai"):
        if len(rest) < 2:
            sys.exit(f"usage: {verb} URL MODEL [KEY_ENV]")
        old = summ
        summ = {"provider": "openai", "base_url": rest[0], "model": rest[1]}
        if len(rest) > 2:
            summ["api_key_env"] = rest[2]
        elif old.get("provider") == "openai":  # keep an existing key setting
            for k in ("api_key", "api_key_env", "extra_body"):
                if k in old:
                    summ[k] = old[k]
        save_summ(f"summarizer: {rest[1]} at {rest[0]}")
    elif verb in ("model", "url", "key-env", "key"):
        if not rest or not summ:
            sys.exit(f"usage: {verb} VALUE (after choosing a provider)")
        if verb in ("url", "key-env", "key") and summ.get("provider") != "openai":
            sys.exit(f"`{verb}` applies to endpoint providers; the claude provider uses your login.")
        field = {"model": "model", "url": "base_url", "key-env": "api_key_env", "key": "api_key"}[verb]
        if verb == "key":
            summ.pop("api_key_env", None)
        if verb == "key-env":
            summ.pop("api_key", None)
        summ[field] = rest[0]
        save_summ(f"{verb}: " + ("(stored)" if verb == "key" else rest[0]))
    elif verb == "thinking":
        if not rest or rest[0] not in ("on", "off"):
            sys.exit("usage: thinking on|off")
        summ.setdefault("extra_body", {})["chat_template_kwargs"] = {"enable_thinking": rest[0] == "on"}
        save_summ(f"thinking {rest[0]}")
    elif verb == "disable":
        summ["enabled"] = False
        save_summ("cards disabled (existing cards are kept and still shown)")
    elif verb == "test":
        cfg = cards.load_config(args.db)
        if not cfg:
            sys.exit("No summarizer configured.")
        try:
            out, model = cards.call_llm(cfg["summarizer"], "Output JSON only.", 'Return {"ok": true}')
            cards.parse_card(out)
            say(f"ok ({model})")
        except Exception as e:  # report whatever went wrong with the endpoint
            say(f"FAILED: {e}")
    elif verb == "now":
        cmd_cards(db, argparse.Namespace(db=args.db, auto=False, all=False, full=False,
                                         session=rest[0] if rest else None, quiet=False))
    else:
        say(CTL_USAGE)


def urllib_errors():
    import urllib.error
    return (urllib.error.URLError, TimeoutError, ConnectionError, OSError)


def cmd_restore(db, args):
    """Put a deleted transcript back from the compressed copy, so it can be resumed."""
    sid = resolve(db, args.session)
    if not sid:
        return
    rows = db.execute("SELECT path, archived FROM files WHERE session_id=? ORDER BY archived DESC",
                      (sid,)).fetchall()
    src = next((r for r in rows if os.path.exists(archive_path(args.db, r["path"]))), None)
    if not src:
        sys.exit("No compressed copy of this session (it was archived before copies were kept).")
    dest = args.to or src["path"]
    if os.path.exists(dest) and not args.to:
        print(f"Transcript is still in place: {dest}")
    else:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with gzip.open(archive_path(args.db, src["path"]), "rb") as f:
            data = f.read()
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        print(f"Restored {len(data) // 1024} KB to {dest}")
    s = db.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    print(f"Resume: {resume_cmd(s)}")


def cmd_resume(db, args):
    sid = resolve(db, args.session)
    if sid:
        print(resume_cmd(db.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()))


def cmd_stats(db, args):
    n_s = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    n_m = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    first = db.execute("SELECT MIN(started) FROM sessions").fetchone()[0]
    gone = sum(1 for r in db.execute("SELECT path FROM files") if not os.path.exists(r[0]))
    print(f"{n_s} sessions, {n_m} messages, since {day(first)}")
    print(f"{gone} source transcripts no longer on disk (kept here)")
    adir = os.path.join(os.path.dirname(args.db), "transcripts")
    asize = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(adir) for f in fs)
    n_arch = sum(len(fs) for _, _, fs in os.walk(adir))
    print(f"compressed transcripts: {n_arch} files, {asize // 2**20} MB in {adir}"
          + ("  (restore with: restore <session>)" if gone else ""))
    print(f"db: {args.db} ({os.path.getsize(args.db) // 2**20} MB)")
    print("\nTop projects:")
    for r in db.execute("""SELECT project, COUNT(*) n, MAX(updated) u FROM sessions
                           GROUP BY project ORDER BY u DESC LIMIT 15"""):
        print(f"  {r['project'] or '?':32} {r['n']:4} sessions, last {day(r['u'])}")


# ---------------------------------------------------------------------- helpers

def resolve(db, key):
    """Session id from a full id, an id prefix, or a session name/title."""
    rows = db.execute(
        "SELECT session_id FROM sessions WHERE session_id LIKE ? ORDER BY updated DESC", (key + "%",)
    ).fetchall()
    if not rows:
        rows = db.execute(
            """SELECT session_id FROM sessions WHERE name = ? OR ai_title = ?
               UNION ALL SELECT session_id FROM sessions WHERE name LIKE ? OR ai_title LIKE ?""",
            (key, key, f"%{key}%", f"%{key}%"),
        ).fetchall()
        rows = list(dict.fromkeys(r[0] for r in rows))
    else:
        rows = [r[0] for r in rows]
    if not rows:
        print(f"No session matches {key!r}.")
        return None
    if len(rows) > 1 and not rows[0].startswith(key) and key not in rows[:1]:
        print(f"{key!r} matches {len(rows)} sessions; using the most recent. Others:", file=sys.stderr)
        for r in rows[1:6]:
            print(f"  {r}", file=sys.stderr)
    return rows[0]


def label(r):
    return r["name"] or r["ai_title"] or "(untitled)"


def resume_cmd(s):
    cwd = s["cwd"]
    cd = f"cd {cwd.replace(HOME, '~', 1)} && " if cwd else ""
    return f"{cd}claude --resume {s['session_id']}"


def day(ts):
    return (ts or "")[:10] or "?"


def trunc(t, n):
    t = t or ""
    return t if len(t) <= n else t[:n] + f" … [+{len(t)-n} chars]"


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------------- main

READ_COMMANDS = {"search", "sessions", "show", "resume", "stats", "card", "cards", "cards-ctl", "restore"}


def main():
    ap = argparse.ArgumentParser(description="Search and resume Claude Code sessions.")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"database path (default {DEFAULT_DB})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("index", help="index new transcript lines (incremental)")
    p.add_argument("path", nargs="*", help=f"transcript roots (default {DEFAULT_SOURCE})")
    p.add_argument("--force", action="store_true", help="re-read files from the start")
    p.add_argument("--cards", action="store_true", help="then update due cards (hooks use this)")
    p.add_argument("-q", "--quiet", action="store_true")

    p = sub.add_parser("reindex", help="catch up now; --full re-reads every transcript")
    p.add_argument("path", nargs="*")
    p.add_argument("--full", dest="force", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")

    p = sub.add_parser("search", help="full-text search over messages")
    p.add_argument("query", nargs="+")
    p.add_argument("-n", "--limit", type=int, default=15)
    p.add_argument("-p", "--project", help="substring of project name or cwd")
    p.add_argument("-s", "--session", help="restrict to one session (id, prefix or name)")
    p.add_argument("--role", choices=["user", "assistant", "summary"])
    p.add_argument("--since", help="e.g. 7d, 4w, 3m or 2026-09-01")
    p.add_argument("--recent", action="store_true", help="newest first instead of best match")
    p.add_argument("--by-session", action="store_true", help="one line per matching session")
    p.add_argument("--raw", action="store_true", help="pass the query to FTS5 unchanged")
    p.add_argument("--cards", action="store_true", help="search summary cards instead of messages")

    p = sub.add_parser("sessions", help="list sessions, most recent first")
    p.add_argument("-n", "--limit", type=int, default=25)
    p.add_argument("--status", action="store_true", help="show each card's status line")
    p.add_argument("-p", "--project")
    p.add_argument("--since")

    p = sub.add_parser("show", help="read a session's messages")
    p.add_argument("session", help="id, id prefix, or name")
    p.add_argument("--around", help="message #id from search; shows context around it")
    p.add_argument("-C", "--context", type=int, default=4)
    p.add_argument("--last", type=int, default=12)
    p.add_argument("--head", type=int)
    p.add_argument("--all", action="store_true")
    p.add_argument("--role", choices=["user", "assistant", "summary"])
    p.add_argument("--full", action="store_true", help="do not truncate messages")
    p.add_argument("-w", "--width", type=int, default=1500)

    p = sub.add_parser("restore", help="put a deleted transcript back from its compressed copy")
    p.add_argument("session")
    p.add_argument("--to", help="write here instead of the original location")

    p = sub.add_parser("resume", help="print the command to resume a session")
    p.add_argument("session")

    sub.add_parser("stats", help="database summary")

    p = sub.add_parser("card", help="a session's summary card, plus files and git it touched")
    p.add_argument("session")
    p.add_argument("--refresh", action="store_true", help="bring the card up to date first")
    p.add_argument("--full", action="store_true", help="rebuild the card from the first message")

    p = sub.add_parser("cards", help="generate cards (default: sessions that are due)")
    p.add_argument("session", nargs="?")
    p.add_argument("--all", action="store_true", help="every session with enough messages")
    p.add_argument("--auto", action="store_true", help="only if auto cards are enabled")
    p.add_argument("--full", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")

    sub.add_parser("config", help="show the summarizer configuration")

    p = sub.add_parser("cards-ctl", help="control summary cards (status/on/off/haiku/local/...)")
    p.add_argument("verb", nargs=argparse.REMAINDER)

    p = sub.add_parser("cards-setup", help="turn session cards on (claude | openai) or off")
    p.add_argument("provider", choices=["claude", "openai", "off"])
    p.add_argument("--model", help="claude: haiku/sonnet/...; openai: the served model name")
    p.add_argument("--base-url", help="openai: e.g. http://localhost:11434/v1 or ${VAR}/v1")
    p.add_argument("--key-env", help="openai: name of the env var holding the API key")
    p.add_argument("--no-thinking", action="store_true", help="openai: disable model thinking (vLLM/Qwen)")
    p.add_argument("--no-test", action="store_true")

    args = ap.parse_args()
    db = connect(args.db)
    if args.cmd in READ_COMMANDS:
        # Catch up before answering: a no-change run only stats each transcript,
        # so the archive is never behind what is on disk.
        run_index(db, args.db, wait=True)
    try:
        {"index": cmd_index, "reindex": cmd_index, "search": cmd_search,
         "sessions": cmd_sessions, "show": cmd_show, "resume": cmd_resume, "stats": cmd_stats,
         "card": cmd_card, "cards": cmd_cards, "config": cmd_config,
         "cards-setup": cmd_cards_setup, "cards-ctl": cmd_cards_ctl,
         "restore": cmd_restore}[args.cmd](db, args)
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
