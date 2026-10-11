"""Text preparation for speech: drop what should never be read aloud, then
split what remains into speakable chunks.

This is the half of the pipeline that has nothing to do with audio, and it is
deliberately dependency-free so it can be unit-tested without a model.
"""
import re

# --- text filtering -------------------------------------------------------
#
# Two passes. First whole *lines* that are never worth hearing get dropped
# (code, tables, diffs, tracebacks, log noise), then what survives gets its
# markdown syntax stripped. Dropping by line beats regex-squeezing prose
# out of a code block after the fact.

_FENCE = re.compile(r"^\s*(```|~~~)")

_DROP_LINE = [
    re.compile(r"^\s*\|"),                                  # table row / delimiter
    re.compile(r"^\s*[+|][-+=| ]{6,}[+|]?\s*$"),            # ascii table border
    re.compile(r"^\s*[-=_*#~]{4,}\s*$"),                    # horizontal rule
    re.compile(r"^\s*(?:\+{1,3}|-{2,3})\s"),                # diff lines ("- " is a bullet)
    re.compile(r"^\s*@@ .* @@"),                             # diff hunk header
    re.compile(r"^\s*(?:File \"|Traceback \(|\s+at .+\(.+:\d+\))"),  # tracebacks
    re.compile(r"^\s*(?:\$|>>>|#)\s+\S"),                   # shell / repl prompts
    re.compile(r"^\s*[\w.]+\s*[:=]\s*[\[{<]"),              # config / literal assignment
    re.compile(r"^\s*</?[a-zA-Z!]"),                         # html/xml line
    re.compile(r"^\s*(?:import|from|def|class|return|const|let|var|function|public|private|"
               r"if|for|while|else|elif|try|except|catch|switch|case|package|use|fn|impl)\b"
               r".*[;{:()\[\]]"),                            # a statement, not a sentence
]

# Punctuation-dense or symbol-heavy lines read as gibberish out loud.
_CODEY = re.compile(r"[{}<>\[\]|=;$#@\\/_*`~^%]")
_WORD = re.compile(r"[A-Za-z]{2,}")


def _is_codey(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) < 12:
        return False
    symbols = len(_CODEY.findall(stripped))
    words = len(_WORD.findall(stripped))
    if symbols >= 4 and symbols > words:
        return True
    return symbols / len(stripped) > 0.22


def _drop(line: str) -> bool:
    if line.startswith("    ") and line.strip():       # indented code block
        return True
    return any(p.search(line) for p in _DROP_LINE) or _is_codey(line)


# The last sentence of a line when it ends in ":" -- "." only ends a sentence
# before whitespace, so "client.py:" stays one word.
_LEAD_IN_TAIL = re.compile(r"(?:^|(?<=[.!?])\s+)(?:(?![.!?]\s).)*:\s*$")


def strip_unspeakable(text: str) -> str:
    """Drop whole lines that are code, tables, diffs or log noise."""
    marks, in_fence = [], False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            marks.append((line, True))
            continue
        marks.append((line, in_fence or _drop(line)))

    # "Here is the diff:" followed by a dropped block is a lead-in to nothing.
    # Only the sentence ending in ":" is the lead-in; what precedes it stays.
    for i, (line, dropped) in enumerate(marks):
        if dropped or not line.rstrip().endswith(":"):
            continue
        for nxt, nxt_dropped in marks[i + 1:]:
            if not nxt.strip():
                continue
            if nxt_dropped:
                rest = _LEAD_IN_TAIL.sub("", line)
                marks[i] = (rest, not rest.strip())
            break

    return "\n".join(line for line, dropped in marks if not dropped)


# Ordered so that structure is stripped before the leftovers are squeezed.
_STRIP = [
    (re.compile(r"```[\s\S]*?```"), " "),                  # fenced code blocks
    (re.compile(r"~~~[\s\S]*?~~~"), " "),
    (re.compile(r"<[^>\n]{1,200}>"), " "),                 # html/xml tags
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), " "),            # images
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),         # links -> label
    (re.compile(r"`([^`]+)`"), r"\1"),                     # inline code
    (re.compile(r"https?://\S+"), " link "),
    (re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.M), ""),          # headers
    (re.compile(r"^[ \t]{0,3}>[ \t]?", re.M), ""),               # blockquotes
    (re.compile(r"^[ \t]*[-*+][ \t]+", re.M), ""),               # bullets
    (re.compile(r"^[ \t]*\|.*\|[ \t]*$", re.M), " "),            # table rows
    (re.compile(r"\*{1,3}([^*]+)\*{1,3}"), r"\1"),         # bold/italic
    (re.compile(r"_{2}([^_]+)_{2}"), r"\1"),
    (re.compile(r"~~([^~]+)~~"), r"\1"),
    (re.compile(r"\[[ xX]\]"), " "),                       # checkboxes
    (re.compile(r"[→←↑↓•·|▶●■◆✓✗✅❌➜»]"), " "),
    (re.compile(r"^[ \t]*[-=_]{3,}[ \t]*$", re.M), " "),         # rules
]

_PATHY = re.compile(r"(?<![\w/])(?:~|\.{1,2})?/[\w.\-/]{4,}")


def clean_for_tts(text: str, max_chars: int = 0) -> str:
    """Turn a markdown answer into something worth hearing out loud."""
    text = strip_unspeakable(text)
    for pattern, repl in _STRIP:
        text = pattern.sub(repl, text)
    text = _PATHY.sub(lambda m: m.group(0).rstrip("/").rsplit("/", 1)[-1], text)
    # Headings and list items rarely end in punctuation; give each its pause.
    text = re.sub(r"([^\s.!?:;,])[ \t]*\n", r"\1.\n", text)
    text = re.sub(r"\n{2,}", ". ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[:;,]\s*\.", ".", text)
    text = re.sub(r"(\.\s*){2,}", ". ", text)
    # A dropped leading block (a table, a code fence) leaves its ". " behind.
    text = re.sub(r"^[\s.,;:!?]+", "", text).strip()

    if max_chars and len(text) > max_chars:
        cut = text[:max_chars]
        # Prefer to stop on a sentence boundary rather than mid-word.
        boundary = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        text = (cut[: boundary + 1] if boundary > max_chars * 0.5 else cut.rsplit(" ", 1)[0]) + " ..."
    return text


# --- sentence chunking ----------------------------------------------------
#
# Kokoro synthesizes a whole paragraph before returning a single sample, so
# a long answer means a long silence. Feeding it sentence-sized chunks lets
# playback start while the rest is still being generated.

_SENTENCE_END = re.compile(r"(?<=[.!?;:])\s+|(?<=[.!?])(?=[\"\')\]])\s+")
# Abbreviations that must not end a chunk (the split would garble prosody).
_ABBREV = re.compile(r"\b(?:e\.g|i\.e|etc|vs|approx|Dr|Mr|Ms|Mrs|St|No|Fig|cf|al)\.$", re.I)

FIRST_CHUNK_CHARS = 110   # short, so the first audio lands fast
CHUNK_CHARS = 260         # then longer, for natural prosody


def split_chunks(text: str, first=FIRST_CHUNK_CHARS, size=CHUNK_CHARS):
    """Split into speakable chunks, smallest first for low time-to-first-audio."""
    pieces, buf = [], ""
    for part in _SENTENCE_END.split(text):
        if not part:
            continue
        buf = f"{buf} {part}".strip() if buf else part
        if _ABBREV.search(buf):
            continue
        pieces.append(buf)
        buf = ""
    if buf:
        pieces.append(buf)

    chunks, current = [], ""
    for piece in pieces:
        limit = first if not chunks else size
        # An over-long sentence still has to go out on its own.
        if current and len(current) + 1 + len(piece) > limit:
            chunks.append(current)
            current = piece
        else:
            current = f"{current} {piece}".strip() if current else piece
        if len(current) >= limit:
            chunks.append(current)
            current = ""
    if current:
        chunks.append(current)
    return [c for c in (c.strip() for c in chunks) if c]
