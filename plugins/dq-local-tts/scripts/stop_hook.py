#!/usr/bin/env python3
"""Hook worker: speak whatever assistant text this turn has produced that has
not been spoken yet.

Runs on two events:
  PreToolUse -- text written before a tool call is spoken as the tool starts,
                instead of waiting for the whole turn to end.
  Stop       -- the rest of the turn, ending with the final reply.

Hooks fire before the transcript catches up: the message that triggered them
is often not flushed yet, and reading the transcript straight away spoke the
*previous* piece -- the plugin ran one step behind. So the final reply comes
from the Stop payload's `last_assistant_message`, and on PreToolUse the worker
waits until the tool call itself (by `tool_use_id`) has reached the transcript,
which means the text written before it has too.

Runs detached from the hook itself, so a cold daemon start never blocks.
"""
import fcntl
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import speak  # noqa: E402
import ttslib  # noqa: E402

MAX_SCAN_LINES = 5000   # a turn never spans more; bounds work on huge transcripts
FLUSH_WAIT_SECONDS = 2.0


def _text_of(content):
    """Concatenated text blocks of a message's content, or ''."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"
                         and b.get("text", "").strip())
    return ""


def _is_prompt(entry):
    """A real user prompt -- not a tool result, meta entry or subagent line."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    return isinstance(content, list) and not any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def turn_texts(transcript_path):
    """(turn_id, [assistant texts in order]) for the current turn.

    Scans backwards to the latest real prompt, so only this turn's lines are
    parsed however long the session has grown.
    """
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, []

    texts, turn_id = [], None
    for line in reversed(lines[-MAX_SCAN_LINES:]):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if _is_prompt(entry):
            turn_id = entry.get("uuid")
            break
        if entry.get("type") == "assistant" and not entry.get("isSidechain"):
            text = _text_of((entry.get("message") or {}).get("content"))
            if text.strip():
                texts.append(text)
    return turn_id, texts[::-1]


def wait_for_tool_use(transcript_path, tool_use_id):
    """Block until the tool call is in the transcript (or give up quietly)."""
    needle = f'"id":"{tool_use_id}"'.encode()
    deadline = time.time() + FLUSH_WAIT_SECONDS
    while time.time() < deadline:
        try:
            with open(transcript_path, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 262144))   # the call is near the end
                if needle in f.read().replace(b'": "', b'":"'):
                    return True
        except OSError:
            return False
        time.sleep(0.05)
    return False


def _digest(text):
    return hashlib.sha1(" ".join(text.split()).encode("utf-8")).hexdigest()[:16]


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0

    cfg = ttslib.load_config()
    if not cfg.get("enabled", True):
        return 0

    session_id = payload.get("session_id", "")
    transcript = payload.get("transcript_path", "")
    if payload.get("tool_use_id"):
        wait_for_tool_use(transcript, payload["tool_use_id"])
    turn_id, texts = turn_texts(transcript)
    final = _text_of(payload.get("last_assistant_message"))
    if final.strip():
        texts.append(final)   # a duplicate of a flushed copy is dropped below

    # Several hooks can fire at once (parallel tool calls, a repeated Stop):
    # serialize, so each piece of text is spoken exactly once and in order.
    ttslib.HOME.mkdir(parents=True, exist_ok=True)
    with open(ttslib.HOME / "hook.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = ttslib.load_state()
        sessions = state.setdefault("turns", {})
        seen = sessions.get(session_id)
        if not seen or seen.get("turn") != turn_id:
            seen = {"turn": turn_id, "spoken": []}

        fresh = []
        for text in texts:
            key = _digest(text)
            if key not in seen["spoken"]:
                seen["spoken"].append(key)
                fresh.append(text)
        if not fresh:
            return 0

        sessions.pop(session_id, None)
        sessions[session_id] = seen
        for stale in list(sessions)[:-50]:   # bound the table across sessions
            sessions.pop(stale, None)
        state.pop("spoken", None)            # pre-0.3 dedup format
        ttslib.save_state(state)

        event = payload.get("hook_event_name", "?")
        for text in fresh:
            cleaned = ttslib.clean_for_tts(text, cfg["max_chars"])
            if not cleaned:
                continue
            print(f"[hook] {event}: {cleaned[:60]}{'...' if len(cleaned) > 60 else ''}", flush=True)
            speak.request({"cmd": "speak", "text": cleaned,
                           "voice": cfg["voice"], "speed": cfg["speed"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
