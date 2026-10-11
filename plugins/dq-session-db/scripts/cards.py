"""Session summary cards.

A card is a small JSON summary of one session (goal, status, decisions,
outcomes, open threads, key facts), each item citing the message #id it came
from. Cards are built incrementally: the card records the last message it
covers, and an update sends only "current card + newer messages" to the model.

Cards are off until the user turns them on with `cards-setup`. Two providers:
  claude  headless `claude -p` with the user's own Claude Code login (e.g. Haiku)
  openai  any OpenAI-compatible endpoint: a local vLLM/Ollama, OpenAI, OpenRouter, ...
"""

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

LIST_CAPS = {"phases": 10, "decisions": 12, "outcomes": 12, "open_threads": 12, "key_facts": 12}

DEFAULT_CARDS = {
    "auto": True,
    "min_messages": 6,
    "min_new_messages": 20,
    "idle_minutes": 30,
    "max_per_run": 5,
    "chunk_chars": 60000,
    "message_chars": 3000,
}

ITEMS = """  "decisions": [{"text": "choices the user made or agreed to", "msg": 123}],
  "outcomes": [{"text": "what was built, fixed, found, or concluded", "msg": 123}],
  "open_threads": [{"text": "unfinished work, pending questions, next steps", "msg": 123}],
  "key_facts": [{"text": "names, numbers, paths, URLs, ids worth finding again", "msg": 123}],"""

SEGMENT = """You summarize one stretch of a Claude Code session (a conversation between a user \
and an AI coding assistant). Messages are prefixed with their #id. Summarize ONLY this \
stretch, as JSON with exactly these keys:

{
  "when": "YYYY-MM-DD of the stretch's main activity",
  "summary": "what happened in this stretch, two to four sentences",
  "projects": ["project or repo names worked on"],
""" + ITEMS + """
}

Rules: "msg" is the #id of the message the item comes from; never invent ids. Decisions are \
the user's choices, not the assistant's suggestions. At most 8 items per list. Be concrete \
and terse; use the user's own terms. Output JSON only."""

SYNTH = """You write the summary card for a whole Claude Code session from the summaries of \
its stretches, given in order. The card lets someone decide in ten seconds what the session \
was about and where it stands, and find the message behind every claim. Output JSON with \
exactly these keys:

{
  "title": "short name covering the session's main work, all of it",
  "goal": "what the user was trying to achieve across the session, one or two sentences",
  "status": "where it stands at the END of the last stretch, one sentence",
  "projects": ["project or repo names worked on"],
  "phases": [{"when": "YYYY-MM-DD", "text": "one line per major stretch or topic", "msg": 123}],
""" + ITEMS + """
  "tags": ["a few topic keywords"]
}

Rules:
- Cover the WHOLE session, first stretch to last. A long session changes topic; each topic \
belongs in title, goal and phases, and its important decisions and outcomes stay.
- Later stretches win on conflicts: drop open threads that a later stretch resolved, and \
record the resolution as an outcome.
- Limits: phases 10, other lists 12 each. Choose what matters most across the whole session; \
merge near-duplicates.
- Keep the "msg" ids from the stretch summaries; never invent ids.
- Output JSON only."""


# ------------------------------------------------------------------- config

def config_path(db_path):
    return os.path.join(os.path.dirname(db_path), "config.json")


def load_config(db_path):
    """The config, or None when cards are off (no file, no summarizer, or disabled)."""
    p = config_path(db_path)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        cfg = json.load(f)
    summ = cfg.get("summarizer")
    if not summ or not summ.get("enabled", True):
        return None
    summ.setdefault("provider", "openai" if summ.get("base_url") else "claude")
    return {"summarizer": summ, "cards": {**DEFAULT_CARDS, **(cfg.get("cards") or {})}}


def read_raw(db_path):
    p = config_path(db_path)
    if not os.path.exists(p):
        return {}
    with open(p) as f:
        return json.load(f)


def write_raw(db_path, cfg):
    """Write config.json readable only by the user: it may hold an API key."""
    p = config_path(db_path)
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    cfg.setdefault("cards", dict(DEFAULT_CARDS))
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.chmod(p, 0o600)
    return p


def write_config(db_path, summarizer):
    cfg = read_raw(db_path)
    cfg["summarizer"] = summarizer
    return write_raw(db_path, cfg)


def expand(v):
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), v)


# ---------------------------------------------------------------------- llm

def call_llm(summ, system, user):
    if summ["provider"] == "claude":
        return call_claude(summ, system, user)
    return call_openai(summ, system, user)


def call_claude(summ, system, user):
    """Headless Claude Code on the user's own login. Replacing the system prompt and
    disabling tools keeps the per-call overhead to a few hundred tokens."""
    cmd = ["claude", "-p", "--model", summ.get("model", "haiku"), "--tools", "",
           "--system-prompt", system, "--no-session-persistence", "--strict-mcp-config",
           "--disable-slash-commands", "--output-format", "json"]
    env = {**os.environ, "SESSION_DB_CHILD": "1"}  # our own hooks skip this child
    try:
        r = subprocess.run(cmd, input=user, capture_output=True, text=True, env=env,
                           timeout=summ.get("timeout", 300))
    except FileNotFoundError:
        raise ConnectionError("`claude` is not on PATH")
    except subprocess.TimeoutExpired:
        raise TimeoutError("claude -p timed out")
    try:
        data = json.loads(r.stdout)
    except ValueError:
        raise ConnectionError(f"claude -p failed: {(r.stderr or r.stdout)[:300]}")
    if data.get("is_error") or data.get("terminal_reason") not in (None, "completed"):
        raise ConnectionError(f"claude -p: {data.get('terminal_reason')}: {str(data.get('result'))[:300]}")
    model = next(iter(data.get("modelUsage") or {}), summ.get("model", "claude"))
    return data.get("result") or "", model


def call_openai(summ, system, user):
    url = expand(summ["base_url"]).rstrip("/") + "/chat/completions"
    body = {
        "model": summ["model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        # A cap turns a rare runaway generation into a fast retry instead of a hang.
        "max_tokens": summ.get("max_tokens", 6000),
    }
    body.update(summ.get("extra_body") or {})
    headers = {"Content-Type": "application/json"}
    key = summ.get("api_key") or os.environ.get(summ.get("api_key_env") or "", "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, json.dumps(body).encode(), headers)
    with urllib.request.urlopen(req, timeout=summ.get("timeout", 300)) as r:
        data = json.load(r)
    return data["choices"][0]["message"]["content"], data.get("model", summ["model"])


def parse_card(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in model output")
    card = json.loads(m.group(0))
    for k in LIST_CAPS:
        card[k] = [i if isinstance(i, dict) else {"text": str(i)} for i in card.get(k) or []]
    return card


def over_caps(card):
    return any(len(card.get(k) or []) > n for k, n in LIST_CAPS.items())


CONDENSE = """You are given a summary card for a long Claude Code session. Some lists are too \
long. Return the same JSON card with each list cut to its limit: {caps}. Keep what matters \
most across the WHOLE session (the arc from start to end, final outcomes, what is still open); \
merge near-duplicates into one item that keeps the most useful "msg" id. Do not add \
information. Output JSON only."""


def condense(summ, card):
    caps = ", ".join(f"{k} {n}" for k, n in LIST_CAPS.items())
    out, _ = call_llm(summ, CONDENSE.format(caps=caps), json.dumps(card, ensure_ascii=False))
    new = parse_card(out)
    for k, n in LIST_CAPS.items():  # the model may still overshoot; the cap is a promise
        new[k] = new[k][:n]
    return new


# -------------------------------------------------------------------- build

def fmt_message(m, limit):
    text = m["text"]
    if len(text) > limit:
        text = text[:limit] + f" …[+{len(text) - limit} chars]"
    return f"#{m['id']} [{(m['ts'] or '')[:16]}] {m['role'].upper()}: {text}"


def chunks(rows, chunk_chars, message_chars):
    batch, size = [], 0
    for m in rows:
        line = fmt_message(m, message_chars)
        if batch and size + len(line) > chunk_chars:
            yield batch
            batch, size = [], 0
        batch.append((m["id"], line))
        size += len(line)
    if batch:
        yield batch


def build_card(db, sid, cfg, full=False, log=print):
    """Summarize new stretches of the session, then rewrite the card from all stretches.

    Two levels on purpose: rewriting one running card batch by batch drifts toward the
    latest batch and forgets the start of long sessions. Stretch summaries are written
    independently and stored; the card is synthesized in one pass over all of them.
    """
    summ, opts = cfg["summarizer"], cfg["cards"]
    if full:
        db.execute("DELETE FROM segments WHERE session_id=?", (sid,))
    after = db.execute("SELECT coalesce(MAX(last_id), 0) FROM segments WHERE session_id=?",
                       (sid,)).fetchone()[0]
    rows = db.execute("SELECT id, ts, role, text FROM messages WHERE session_id=? AND id>? ORDER BY id",
                      (sid, after)).fetchall()
    card_row = db.execute("SELECT covers_through FROM cards WHERE session_id=?", (sid,)).fetchone()
    if not rows and card_row and not full:
        return False
    s = db.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    header = (f"Session {sid}, named \"{s['name'] or s['ai_title'] or 'untitled'}\", started in "
              f"{s['cwd'] or '?'}, {(s['started'] or '?')[:10]} to {(s['updated'] or '?')[:10]}.")
    batches = list(chunks(rows, opts["chunk_chars"], opts["message_chars"]))
    model = summ.get("model")
    for i, batch in enumerate(batches, 1):
        log(f"  {sid[:8]}: stretch {i}/{len(batches)} ({len(batch)} messages)")
        prompt = f"{header}\n\nMessages:\n\n" + "\n\n".join(line for _, line in batch)
        seg, model = ask_json(summ, SEGMENT, prompt)
        seg_no = db.execute("SELECT coalesce(MAX(seg_no), 0) + 1 FROM segments WHERE session_id=?",
                            (sid,)).fetchone()[0]
        db.execute("INSERT INTO segments VALUES (?,?,?,?,?)",
                   (sid, seg_no, batch[0][0], batch[-1][0], json.dumps(seg, ensure_ascii=False)))
        db.commit()  # per stretch: progress survives an interruption
    segs = db.execute("SELECT * FROM segments WHERE session_id=? ORDER BY seg_no", (sid,)).fetchall()
    if not segs:
        return False
    log(f"  {sid[:8]}: card from {len(segs)} stretch summaries")
    body = "\n\n".join(f"Stretch {r['seg_no']} (messages #{r['first_id']}-#{r['last_id']}):\n{r['summary']}"
                        for r in segs)
    card, model = ask_json(summ, SYNTH, f"{header}\n\n{body}")
    if over_caps(card):
        card = condense(summ, card)
    save(db, sid, card, segs[-1]["last_id"], model)
    return True


def ask_json(summ, system, prompt):
    for attempt in (1, 2):
        try:
            out, model = call_llm(summ, system, prompt)
            return parse_card(out), model
        except (ValueError, json.JSONDecodeError) as e:
            if attempt == 2:
                raise RuntimeError(f"model returned unusable JSON: {e}")


def save(db, sid, card, last_id, model):
    n = db.execute("SELECT COUNT(*) FROM messages WHERE session_id=? AND id<=?",
                   (sid, last_id)).fetchone()[0]
    text = card_text(card)
    db.execute("INSERT OR REPLACE INTO cards VALUES (?,?,?,?,?,?,?)",
               (sid, json.dumps(card, ensure_ascii=False), text, last_id, n,
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), model))
    db.execute("DELETE FROM cards_fts WHERE session_id=?", (sid,))
    db.execute("INSERT INTO cards_fts(session_id, text) VALUES (?,?)", (sid, text))
    db.commit()


def card_text(card):
    parts = [card.get("title", ""), card.get("goal", ""), card.get("status", ""),
             " ".join(card.get("projects") or []), " ".join(card.get("tags") or [])]
    for k in ("phases", "decisions", "outcomes", "open_threads", "key_facts"):
        parts += [i.get("text", "") for i in card.get(k) or [] if isinstance(i, dict)]
    return "\n".join(p for p in parts if p)


# ------------------------------------------------------------------ auto

def due_sessions(db, opts):
    idle = (datetime.now(timezone.utc) - timedelta(minutes=opts["idle_minutes"])).strftime(
        "%Y-%m-%dT%H:%M:%S")
    rows = db.execute(
        """SELECT s.session_id, s.updated,
                  (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.session_id) AS total,
                  (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.session_id
                     AND m.id > coalesce(c.covers_through, 0)) AS new,
                  (SELECT MAX(ts) FROM messages m WHERE m.session_id = s.session_id) AS last_msg
           FROM sessions s LEFT JOIN cards c ON c.session_id = s.session_id
           ORDER BY s.updated DESC""").fetchall()
    due = []
    for r in rows:
        if not r["new"] or (r["last_msg"] or "") > idle:
            continue  # nothing new, or still active: wait until it goes quiet
        has_card = r["total"] != r["new"]
        if (not has_card and r["total"] >= opts["min_messages"]) or r["new"] >= opts["min_new_messages"]:
            due.append(r["session_id"])
    return due


# ----------------------------------------------------------------- render

def render(db, sid):
    row = db.execute("SELECT * FROM cards WHERE session_id=?", (sid,)).fetchone()
    s = db.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    out = []
    if row:
        c = json.loads(row["card"])
        newer = db.execute("SELECT COUNT(*), MAX(ts) FROM messages WHERE session_id=? AND id>?",
                           (sid, row["covers_through"])).fetchone()
        out.append(f"# {c.get('title') or s['name'] or s['ai_title']}")
        out.append(f"Goal: {c.get('goal', '')}")
        out.append(f"Status: {c.get('status', '')}")
        if c.get("projects"):
            out.append(f"Projects: {', '.join(c['projects'])}")
        if c.get("phases"):
            out.append("\nTimeline:")
            out += [f"  {i.get('when', '')}  {i.get('text', '')}" + (f"  (#{i['msg']})" if i.get("msg") else "")
                    for i in c["phases"] if isinstance(i, dict)]
        for k, label in (("decisions", "Decisions"), ("outcomes", "Outcomes"),
                         ("open_threads", "Open threads"), ("key_facts", "Key facts")):
            items = c.get(k) or []
            if items:
                out.append(f"\n{label}:")
                out += [f"  - {i.get('text', '')}" + (f"  (#{i['msg']})" if i.get("msg") else "")
                        for i in items]
        stale = (f"; {newer[0]} newer messages up to {(newer[1] or '')[:10]} — `card {sid[:8]} --refresh`"
                 if newer[0] else " (current)")
        out.append(f"\nCard: {row['generated_at'][:16]} by {row['model']}, covers through "
                   f"#{row['covers_through']}{stale}")
    else:
        out.append(f"# {s['name'] or s['ai_title'] or '(untitled)'}\n(no card yet)")
    touches = db.execute("SELECT kind, value FROM touches WHERE session_id=? ORDER BY last_ts DESC",
                         (sid,)).fetchall()
    files = [t["value"] for t in touches if t["kind"] == "file"]
    gits = [t["value"] for t in touches if t["kind"] == "git"]
    if files:
        home = os.path.expanduser("~")
        shown = [f.replace(home, "~", 1) for f in files[:25]]
        out.append(f"\nFiles written ({len(files)}):\n  " + "\n  ".join(shown)
                   + (f"\n  … +{len(files) - 25} more" if len(files) > 25 else ""))
    if gits:
        out.append(f"\nGit ({len(gits)}):\n  " + "\n  ".join(g for g in gits[:10]))
    cwd = s["cwd"]
    home = os.path.expanduser("~")
    cd = f"cd {cwd.replace(home, '~', 1)} && " if cwd else ""
    out.append(f"\nResume (copy exactly; needs the full id): {cd}claude --resume {sid}")
    return "\n".join(out)


SETUP_HELP = """Session cards are OFF. Search, show and resume work without them.
Turn them on with one of:

  sessiondb.py cards-setup claude --model haiku
      headless `claude -p` on your own Claude Code login; uses your plan's usage

  sessiondb.py cards-setup openai --base-url URL --model NAME [--key-env VAR] [--no-thinking]
      any OpenAI-compatible endpoint: local vLLM/Ollama, OpenAI, OpenRouter, ...
      e.g. --base-url http://localhost:11434/v1 --model qwen3:8b   (Ollama)

  sessiondb.py cards-setup off"""


def config_status(db_path, cfg):
    if not cfg:
        return SETUP_HELP
    summ, c = cfg["summarizer"], cfg["cards"]
    if summ["provider"] == "claude":
        where = f"claude -p --model {summ.get('model', 'haiku')} (your Claude Code login)"
    else:
        env = summ.get("api_key_env")
        if summ.get("api_key"):
            keyinfo = "stored in config"
        elif env:
            keyinfo = f"${env}" + ("" if os.environ.get(env) else ", NOT SET")
        else:
            keyinfo = "none"
        where = f"{summ['model']} at {expand(summ['base_url'])} (key: {keyinfo})"
    return (f"config: {config_path(db_path)}\nsummarizer: {where}\n"
            f"auto cards: {'on' if c['auto'] else 'off'} (after {c['idle_minutes']} idle min; "
            f"first card at {c['min_messages']} msgs, refresh after {c['min_new_messages']} new)")
