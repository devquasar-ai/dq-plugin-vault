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
import shutil
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
# Our data lives outside ~/.claude on purpose: that folder belongs to Claude Code, which
# cleans it up and may reset it; the archive exists to outlive exactly that.
DATA_HOME = os.environ.get("SESSION_DB_HOME") or os.path.join(
    os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share"), "session-db")
DEFAULT_DB = os.environ.get("SESSION_DB", os.path.join(DATA_HOME, "sessions.db"))
LEGACY_HOME = os.path.join(HOME, ".claude", "session-db")  # used by versions before 0.4
MAX_TEXT = 20000  # per message; long pastes are truncated, not dropped
SCHEMA_VERSION = 4

# Harness-injected text that is noise for search.
NOISE_BLOCK = re.compile(
    r"<(system-reminder|task-notification|local-command-stdout|local-command-stderr)>.*?</\1>",
    re.S,
)
COMMAND_TAG = re.compile(r"</?(command-name|command-message|command-args)>")


# --------------------------------------------------------------------------- db

def stored_schema(path):
    """Schema version of an existing database, or None for a new or empty one."""
    probe = sqlite3.connect(path, timeout=30)
    try:
        row = probe.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row:
            return int(row[0])
        has_tables = probe.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return 0 if has_tables else None
    except sqlite3.OperationalError:
        return None
    finally:
        probe.close()


def guard_and_backup(path):
    """Refuse a database written by a newer session-db; back up one we are about to upgrade."""
    version = stored_schema(path)
    if version is None or version == SCHEMA_VERSION:
        return
    if version > SCHEMA_VERSION:
        sys.exit(f"{path} was written by a newer session-db (schema v{version}; this code knows "
                 f"v{SCHEMA_VERSION}). Update the plugin; the database is left untouched.")
    backup = f"{path}.pre-v{SCHEMA_VERSION}.bak"
    if not os.path.exists(backup):
        src, dst = sqlite3.connect(path, timeout=30), sqlite3.connect(backup)
        try:
            src.backup(dst)  # consistent copy, WAL included
        finally:
            src.close()
            dst.close()
        os.chmod(backup, 0o600)


def migrate_legacy_home(db_path):
    """Move a pre-0.4 archive out of ~/.claude into DATA_HOME, once.

    Copy, verify, then rename the old folder instead of deleting it, so nothing is lost if
    anything goes wrong. Only for the default location: an explicit --db/SESSION_DB is
    the user's choice.
    """
    legacy_db = os.path.join(LEGACY_HOME, "sessions.db")
    if db_path != os.path.join(DATA_HOME, "sessions.db") or os.path.exists(db_path) \
            or not os.path.exists(legacy_db):
        return
    os.makedirs(DATA_HOME, mode=0o700, exist_ok=True)
    os.chmod(DATA_HOME, 0o700)
    with open(os.path.join(LEGACY_HOME, "sessions.db.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # no indexer may write mid-copy
        tmp = db_path + ".moving"
        src, dst = sqlite3.connect(legacy_db, timeout=30), sqlite3.connect(tmp)
        try:
            src.backup(dst)
            n_old = src.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            n_new = dst.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            src.close()
            dst.close()
        if n_old != n_new:
            os.remove(tmp)
            sys.exit(f"Moving the archive to {DATA_HOME} failed verification; nothing changed.")
        for name in os.listdir(LEGACY_HOME):
            if name.startswith("sessions.db"):
                continue  # the database is copied above; WAL, locks and backups stay behind
            src_path = os.path.join(LEGACY_HOME, name)
            if os.path.isdir(src_path):
                shutil.copytree(src_path, os.path.join(DATA_HOME, name), dirs_exist_ok=True)
            else:
                shutil.copy2(src_path, os.path.join(DATA_HOME, name))
        os.chmod(tmp, 0o600)
        os.replace(tmp, db_path)
    os.rename(LEGACY_HOME, LEGACY_HOME + ".migrated")
    print(f"session-db: moved the archive from {LEGACY_HOME} to {DATA_HOME} (outside Claude "
          f"Code's folder). The old copy is kept at {LEGACY_HOME}.migrated; delete it when "
          f"you're happy.", file=sys.stderr)


def connect(path):
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    new = not os.path.exists(path)
    if not new:
        guard_and_backup(path)
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
    scols = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
    if "hidden" not in scols:
        # v4: hidden sessions, removed-session tombstones, and lookup detection. Re-read
        # every transcript to find lookups, keeping archive progress (offset 0, size -1
        # forces the re-read; `archived` stays, so copies are not rewritten).
        db.execute("ALTER TABLE sessions ADD COLUMN hidden INTEGER DEFAULT 0")
        db.execute("ALTER TABLE sessions ADD COLUMN hidden_reason TEXT")
        db.execute("UPDATE files SET offset=0, size=-1")
    db.execute("""CREATE TABLE IF NOT EXISTS removed (
        session_id TEXT PRIMARY KEY, name TEXT, removed_at TEXT, backup TEXT)""")
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
        if is_lookup(name, inp):
            touch(db, sid, "lookup", "session-db", ts)


def is_lookup(name, inp):
    """Did this tool call use session-db itself? Such sessions are mostly 'find that session'."""
    if name == "Skill":
        return "session-db" in str(inp.get("skill") or inp.get("command") or "")
    if name == "Bash":
        return bool(re.search(r"sessiondb\.py|cards-ctl\.sh", inp.get("command") or ""))
    return False


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
    root = os.path.join(os.path.dirname(db_path), "transcripts")
    os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)  # makedirs' mode is filtered by the umask; enforce it
    os.chmod(os.path.dirname(dest), 0o700)
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
    if db.execute("SELECT 1 FROM removed WHERE session_id=?",
                  (os.path.splitext(os.path.basename(path))[0],)).fetchone():
        return 0  # removed by the user; never bring it back
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

HOWTO = """This folder is the session-db archive of Claude Code sessions.

Don't query sessions.db directly or grep the transcripts. Use the tool, which keeps the
index current and searches it properly:

  python3 "{script}" search "<words>" --by-session     which sessions mention it
  python3 "{script}" search "<words>"                  matching messages with #ids
  python3 "{script}" show <session> --around <#id>     read around a hit
  python3 "{script}" sessions --status                 recent sessions
  python3 "{script}" card <session>                    summary, files and commits
  python3 "{script}" resume <session>                  command to resume it

<session> is an id, an id prefix, or a session name. Plain words are ANDed.

If you use a local model, calling the skill directly gives you the best results:
  /dq-session-db:sessions <question>
"""


def write_howto(db_path):
    """Leave usage instructions next to the archive, for agents that find the folder
    without loading the skill. Refreshed so the script path stays current."""
    p = os.path.join(os.path.dirname(db_path), "HOW-TO-SEARCH.txt")
    text = HOWTO.format(script=os.path.abspath(__file__))
    try:
        with open(p) as f:
            if f.read() == text:
                return
    except OSError:
        pass
    try:
        with open(p, "w") as f:
            f.write(text)
    except OSError:
        pass


def run_index(db, db_path, roots=None, force=False, wait=False):
    """Index new transcript lines. Returns (files, added) or None if another run holds the lock."""
    lock = open(db_path + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except OSError:
        return None
    host = socket.gethostname().split(".")[0]
    write_howto(db_path)
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
        cmd_cleanup(db, argparse.Namespace(db=args.db, auto=True, sessions=None,
                                           remove=False, hide=False, yes=False))
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
    where, params = visibility(where, params, args, "s")
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
        # Grouping by session needs many more message hits than sessions shown, or one chatty
        # session can fill the whole limit and push better-matching sessions off the list.
        rows = db.execute(sql, params + [args.limit * 50 if args.by_session else args.limit]).fetchall()
    except sqlite3.OperationalError as e:
        sys.exit(f"Bad query {q!r}: {e}. Use --raw for FTS5 syntax, or plain words.")
    if not rows:
        print("No matches.")
        hidden_lookup_note(db, q, args)
        return
    if args.by_session:
        seen = {}
        for r in rows:
            seen.setdefault(r["session_id"], []).append(r)
        # A session that keeps coming back to the topic is the one about it; break ties
        # by the best single match (insertion order follows relevance).
        order = {sid: i for i, sid in enumerate(seen)}
        lookup = {sid: lookup_only(db, sid) for sid in seen}
        # A session that only searched for the topic is not about it: rank real work first.
        ranked = sorted(seen.items(), key=lambda kv: (lookup[kv[0]], -len(kv[1]), order[kv[0]]))
        for sid, hits in ranked[: args.limit]:
            r = hits[0]
            tag = "  [lookup: only searched for this]" if lookup[sid] else ""
            print(f"{sid}  {label(r)}  [{r['project'] or '?'}]  {len(hits)} hit(s), "
                  f"latest {day(max(h['ts'] or '' for h in hits))}{tag}")
        hidden_lookup_note(db, q, args)
        return
    for r in rows:
        snip = " ".join(r["snip"].split())
        print(f"#{r['id']}  {day(r['ts'])}  {r['session_id'][:8]}  {label(r)}  [{r['project'] or '?'}]  {r['role']}")
        print(f"    {snip}")
    hidden_lookup_note(db, q, args)
    print("\nRead around a hit: sessiondb.py show <session> --around <#id>")


def search_cards(db, q, args):
    try:
        rows = db.execute(
            """SELECT f.session_id, s.name, s.ai_title, s.project, s.updated, c.card,
                      snippet(cards_fts, 1, '[', ']', ' … ', 24) AS snip
               FROM cards_fts f JOIN sessions s ON s.session_id = f.session_id
               JOIN cards c ON c.session_id = f.session_id
               WHERE cards_fts MATCH ? AND {cond} ORDER BY bm25(cards_fts) LIMIT ?""".format(
                cond=" AND ".join(visibility([], [], args, "s")[0]) or "1"),
            [q] + visibility([], [], args, "s")[1] + [args.limit]).fetchall()
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


# Sessions that searched with session-db and produced nothing: they mention what they looked
# for, but they are not about it. Left out of search results unless asked for.
LOOKUP_ONLY_SQL = """NOT (EXISTS (SELECT 1 FROM touches t WHERE t.session_id = {a}.session_id
                                 AND t.kind = 'lookup')
         AND NOT EXISTS (SELECT 1 FROM touches t WHERE t.session_id = {a}.session_id
                                 AND t.kind IN ('file', 'git')))"""


def hidden_lookup_note(db, q, args):
    if getattr(args, "include_lookups", False):
        return
    n = db.execute(
        f"""SELECT COUNT(DISTINCT m.session_id) FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.session_id = m.session_id
            WHERE messages_fts MATCH ? AND NOT {LOOKUP_ONLY_SQL.format(a='s')}""", (q,)).fetchone()[0]
    if n:
        print(f"({n} lookup session(s) that only searched for this not shown; add --include-lookups)")


def lookup_only(db, sid):
    """True for a session that used session-db and produced nothing (no files, no commits)."""
    kinds = {r[0] for r in db.execute("SELECT DISTINCT kind FROM touches WHERE session_id=?", (sid,))}
    return "lookup" in kinds and not kinds & {"file", "git"}


def visibility(where, params, args, alias):
    """Hide hidden sessions (unless --all) and the session doing the searching."""
    where, params = list(where), list(params)
    if not getattr(args, "all", False):
        where.append(f"coalesce({alias}.hidden, 0) = 0")
    if not getattr(args, "include_lookups", False):
        where.append(LOOKUP_ONLY_SQL.format(a=alias))
    current = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if current and not getattr(args, "include_current", False):
        where.append(f"{alias}.session_id != ?")
        params.append(current)
    return where, params


def cmd_sessions(db, args):
    where, params = [], []
    if not args.all:
        where.append("coalesce(hidden, 0) = 0")
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


# ------------------------------------------------------------------ cleanup

CLEANUP_DEFAULTS = {"auto": "off", "idle_hours": 24,
                    "tiny_max_user_messages": 3, "tiny_max_chars": 3000,
                    "lookup_max_user_messages": 6}


def cleanup_config(db_path):
    return {**CLEANUP_DEFAULTS, **(cards.read_raw(db_path).get("cleanup") or {})}


def cleanup_candidates(db, opts):
    """Quiet sessions with no lasting output: lookups (used session-db, wrote nothing) and
    tiny ones (few user messages, wrote nothing). Never the current session."""
    idle = (datetime.now(timezone.utc) - timedelta(hours=opts["idle_hours"])).strftime(
        "%Y-%m-%dT%H:%M:%S")
    rows = db.execute(
        """SELECT s.*, (SELECT COUNT(*) FROM touches t WHERE t.session_id=s.session_id
                         AND t.kind IN ('file','git')) AS outputs,
                  (SELECT COUNT(*) FROM touches t WHERE t.session_id=s.session_id
                         AND t.kind='lookup') AS lookups,
                  (SELECT coalesce(SUM(length(text)), 0) FROM messages m
                         WHERE m.session_id=s.session_id) AS chars
           FROM sessions s WHERE coalesce(s.hidden, 0) = 0 AND coalesce(s.updated, '') < ?
           ORDER BY s.updated DESC""", (idle,)).fetchall()
    current = os.environ.get("CLAUDE_CODE_SESSION_ID")
    out = []
    for r in rows:
        if r["session_id"] == current or r["outputs"]:
            continue
        n_user = r["n_user"] or 0
        # Both rules ask for a small conversation: a two-question session with long, careful
        # answers is real work, and so is a long session that searched once along the way.
        if r["lookups"] and n_user <= opts["lookup_max_user_messages"]:
            out.append((r, f"lookup: used session-db, {n_user} message(s), wrote nothing"))
        elif n_user <= opts["tiny_max_user_messages"] and r["chars"] < opts["tiny_max_chars"]:
            out.append((r, f"tiny: {n_user} message(s), {r['chars']} chars, wrote nothing"))
    return out


def set_hidden(db, sids, hidden, reason=None):
    db.executemany("UPDATE sessions SET hidden=?, hidden_reason=? WHERE session_id=?",
                   [(1 if hidden else 0, reason if hidden else None, sid) for sid in sids])
    db.commit()


def remove_sessions(db, db_path, sids):
    """Delete sessions from the archive, after backing up the database and moving their
    compressed transcripts aside. Tombstones keep them from being indexed again."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(os.path.dirname(db_path), "backups", f"removed-{stamp}")
    os.makedirs(bdir, mode=0o700, exist_ok=True)
    os.chmod(os.path.dirname(bdir), 0o700)
    src, dst = db, sqlite3.connect(os.path.join(bdir, "sessions.db"))
    try:
        src.backup(dst)
    finally:
        dst.close()
    os.chmod(os.path.join(bdir, "sessions.db"), 0o600)
    for sid in sids:
        name = db.execute("SELECT coalesce(name, ai_title) FROM sessions WHERE session_id=?",
                          (sid,)).fetchone()
        for f in db.execute("SELECT path FROM files WHERE session_id=?", (sid,)).fetchall():
            a = archive_path(db_path, f["path"])
            if os.path.exists(a):
                shutil.move(a, os.path.join(bdir, os.path.basename(a)))
        for table in ("messages", "touches", "segments", "cards", "files"):
            db.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))
        db.execute("DELETE FROM cards_fts WHERE session_id=?", (sid,))
        db.execute("DELETE FROM sessions WHERE session_id=?", (sid,))
        db.execute("INSERT OR REPLACE INTO removed VALUES (?,?,?,?)",
                   (sid, name[0] if name else None, now_iso(), bdir))
    db.commit()
    return bdir


def cmd_cleanup(db, args):
    opts = cleanup_config(args.db)
    if args.auto:  # from the hook: only ever hides, and only if the user turned it on
        if opts["auto"] == "hide":
            found = cleanup_candidates(db, opts)
            set_hidden(db, [r["session_id"] for r, _ in found], True, "auto")
        return
    if args.sessions:
        sids = [x for x in (resolve(db, k) for k in args.sessions) if x]
        found = [(db.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone(),
                  "named by you") for sid in sids]
    else:
        found = cleanup_candidates(db, opts)
    if not found:
        print("Nothing to clean up.")
        return
    for r, why in found:
        print(f"{r['session_id']}  {day(r['updated'])}  {label(r)[:40]:40}  {why}")
    if args.remove:
        if not args.yes:
            print(f"\n{len(found)} session(s) would be REMOVED (deleted after a backup). "
                  "Re-run with --yes to confirm.")
            return
        bdir = remove_sessions(db, args.db, [r["session_id"] for r, _ in found])
        print(f"\nRemoved {len(found)} session(s). Backup: {bdir}")
    elif args.hide:
        set_hidden(db, [r["session_id"] for r, _ in found], True, "cleanup")
        print(f"\nHid {len(found)} session(s). They stay in the archive; see them with "
              "`sessions --all`, bring one back with `unhide <session>`.")
    else:
        print(f"\n{len(found)} candidate(s). Nothing changed. Use --hide (reversible) or "
              "--remove --yes (deletes, after a backup).")


def cmd_unhide(db, args):
    sids = [x for x in (resolve(db, k) for k in args.sessions) if x]
    set_hidden(db, sids, False)
    print(f"Unhid {len(sids)} session(s).")


def cmd_cleanup_auto(db, args):
    raw = cards.read_raw(args.db)
    raw.setdefault("cleanup", dict(CLEANUP_DEFAULTS))["auto"] = "hide" if args.mode == "on" else "off"
    cards.write_raw(args.db, raw)
    print(f"automatic cleanup: {'hide quiet lookup/tiny sessions' if args.mode == 'on' else 'off'}"
          " (removing always needs you)")


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
    if os.path.isdir(LEGACY_HOME + ".migrated"):
        print(f"old pre-0.4 copy still at {LEGACY_HOME}.migrated (safe to delete once you're happy)")
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

READ_COMMANDS = {"search", "sessions", "show", "resume", "stats", "card", "cards", "cards-ctl",
                 "restore", "cleanup", "unhide"}


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
    p.add_argument("--all", action="store_true", help="include hidden sessions")
    p.add_argument("--include-current", action="store_true",
                   help="include the session doing the searching")
    p.add_argument("--include-lookups", action="store_true",
                   help="include sessions that only searched for this")
    p.add_argument("--cards", action="store_true", help="search summary cards instead of messages")

    p = sub.add_parser("sessions", help="list sessions, most recent first")
    p.add_argument("-n", "--limit", type=int, default=25)
    p.add_argument("--status", action="store_true", help="show each card's status line")
    p.add_argument("--all", action="store_true", help="include hidden sessions")
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

    p = sub.add_parser("cleanup", help="list quiet lookup/tiny sessions; --hide or --remove them")
    p.add_argument("sessions", nargs="*", help="act on these instead of the candidates")
    p.add_argument("--hide", action="store_true", help="hide them (reversible)")
    p.add_argument("--remove", action="store_true", help="delete them, after a backup")
    p.add_argument("--yes", action="store_true", help="confirm --remove")
    p.add_argument("--auto", action="store_true", help=argparse.SUPPRESS)

    p = sub.add_parser("unhide", help="bring hidden sessions back")
    p.add_argument("sessions", nargs="+")

    p = sub.add_parser("cleanup-auto", help="automatic hiding of quiet lookup/tiny sessions")
    p.add_argument("mode", choices=["on", "off"])

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
    migrate_legacy_home(args.db)
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
         "restore": cmd_restore, "cleanup": cmd_cleanup, "unhide": cmd_unhide,
         "cleanup-auto": cmd_cleanup_auto}[args.cmd](db, args)
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
