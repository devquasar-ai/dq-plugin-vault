"""The text engine is the part most likely to silently regress, and it needs
no model to test."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kokoro_server import textproc  # noqa: E402


def spoken(text, **kw):
    return textproc.clean_for_tts(text, kw.get("max_chars", 1200))


def test_drops_fenced_code():
    out = spoken("Here is it:\n\n```python\nx = compute(1)\n```\n\nAll good.")
    assert "compute" not in out and "All good." in out


def test_drops_tables_and_rules():
    out = spoken("Results:\n\n| file | change |\n|------|--------|\n| a.py | fix |\n\nDone.")
    assert "|" not in out and "a.py" not in out and "Done." in out


def test_drops_diffs_and_tracebacks():
    out = spoken('Change:\n\n+   added = 1\n-   removed = 2\n\n'
                 'Traceback (most recent call last):\n  File "/x/y.py", line 3\n\nFixed.')
    assert "added" not in out and "Traceback" not in out and "Fixed." in out


def test_drops_html():
    assert "alert" not in spoken('<div class="alert">hi</div>\n\nPlain text.')


def test_dangling_lead_in_removed():
    # "Here is the diff:" introduces content we just deleted, so it must go too.
    out = spoken("Here is the diff:\n\n+ added line\n\nIt works now.")
    assert "diff" not in out and "It works now." in out


def test_paths_collapse_to_basename():
    assert "client.py" in spoken("Edited /Users/me/proj/src/client.py today.")
    assert "/Users" not in spoken("Edited /Users/me/proj/src/client.py today.")


def test_no_stray_period_after_dropped_leading_block():
    text = "| a | b |\n|---|---|\n| 1 | 2 |\n\nEach bump lets the update show up."
    assert spoken(text) == "Each bump lets the update show up."


def test_keeps_markdown_bullets():
    text = "Two options:\n\n- **Reindex** on every search.\n- Keep the hook anyway."
    assert spoken(text) == "Two options. Reindex on every search. Keep the hook anyway."


def test_heading_gets_a_pause_before_the_next_one():
    assert spoken("## 1. First\n\n```\ncode\n```\n\n## 2. Second") == "1. First. 2. Second"


def test_long_paragraph_before_dropped_block_keeps_all_but_the_lead_in():
    para = "It checks file sizes only, so a run takes well under a second on any realistic archive. So:"
    out = spoken(para + "\n```\nx = 1\n```")
    assert out.startswith("It checks file sizes only") and not out.endswith("So.")


def test_lead_in_sentence_with_a_filename_is_dropped_whole():
    assert spoken("Fixed it. The change was in client.py:\n```py\nx = 1\n```") == "Fixed it."


def test_keeps_ordinary_prose():
    out = spoken("The retry loop was off by one. It now stops after three attempts.")
    assert out == "The retry loop was off by one. It now stops after three attempts."


def test_truncates_on_sentence_boundary():
    out = spoken("One two three four five. " * 40, max_chars=100)
    assert len(out) <= 120 and out.endswith("...")


def test_split_puts_short_chunk_first():
    chunks = textproc.split_chunks("First one. " + "Then a much longer sentence here. " * 12)
    assert len(chunks) > 1
    assert len(chunks[0]) <= textproc.FIRST_CHUNK_CHARS


def test_split_does_not_break_abbreviations():
    chunks = textproc.split_chunks("Run it, e.g. with pytest, and then tell me what happened.")
    assert not any(c.rstrip().endswith("e.g.") for c in chunks)


def test_split_covers_all_text():
    text = "Alpha beta. Gamma delta. Epsilon zeta. Eta theta."
    assert "".join(textproc.split_chunks(text)).replace(" ", "") == text.replace(" ", "")


def test_empty_input_is_safe():
    assert spoken("") == ""
    assert textproc.split_chunks("") == []
