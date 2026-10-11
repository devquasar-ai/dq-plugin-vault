"""End-to-end tests for sessiondb.py: run the CLI against throwaway transcripts.

python3 -m unittest discover -s plugins/session-db/tests
"""
import gzip
import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "sessiondb.py")
SID = "aaaa1111-0000-0000-0000-000000000000"
sys.path.insert(0, os.path.dirname(SCRIPT))
import sessiondb  # noqa: E402


def event(kind, n, text, **extra):
    ev = {"type": kind, "uuid": f"u{n}", "sessionId": SID, "cwd": "/tmp/proj",
          "timestamp": f"2026-10-10T10:{n:02d}:00Z", "message": {"content": text}}
    ev.update(extra)
    return json.dumps(ev) + "\n"


class SessionDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.src = os.path.join(root, "projects")
        self.proj = os.path.join(self.src, "-tmp-proj")
        os.makedirs(self.proj)
        self.transcript = os.path.join(self.proj, SID + ".jsonl")
        self.db = os.path.join(root, "db", "sessions.db")
        self.env = {**os.environ, "SESSION_DB_SOURCE": self.src, "SESSION_DB": self.db}

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, *lines, mode="a"):
        with open(self.transcript, mode) as f:
            f.write("".join(lines))

    def run_cli(self, *args):
        r = subprocess.run([sys.executable, SCRIPT, *args], env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def archive(self):
        return sessiondb.archive_path(self.db, self.transcript)

    def test_search_finds_message_and_strips_reminders(self):
        self.write(event("user", 1, "how do we roll back the zebra migration"),
                   event("assistant", 2, "use the down script <system-reminder>secret</system-reminder>"))
        self.assertIn("[zebra]", self.run_cli("search", "zebra"))
        self.assertIn("No matches", self.run_cli("search", "secret"))

    def test_partial_line_waits_for_next_run(self):
        self.write(event("user", 1, "first giraffe"), '{"type": "user", "partial')
        self.assertIn("No matches", self.run_cli("search", "partial"))
        self.write('": 1}\n')
        self.run_cli("index", "-q")
        self.assertIn("giraffe", self.run_cli("search", "giraffe"))

    def test_reindex_does_not_duplicate(self):
        self.write(event("user", 1, "unique okapi"))
        self.run_cli("index", "-q")
        self.run_cli("reindex", "--full", "-q")
        out = self.run_cli("search", "okapi")
        self.assertEqual(out.count("okapi"), 1, out)

    def test_name_resolves_and_resume_includes_cd(self):
        self.write(event("user", 1, "hello"),
                   json.dumps({"type": "custom-title", "customTitle": "my-work", "sessionId": SID}) + "\n")
        self.assertEqual(self.run_cli("resume", "my-work").strip(), f"cd /tmp/proj && claude --resume {SID}")

    def test_archive_grows_and_matches_complete_lines(self):
        self.write(event("user", 1, "one"))
        self.run_cli("index", "-q")
        self.write(event("assistant", 2, "two"), '{"partial')
        self.run_cli("index", "-q")
        with open(self.transcript, "rb") as f:
            data = f.read()
        complete = data[: data.rfind(b"\n") + 1]
        with gzip.open(self.archive(), "rb") as f:
            self.assertEqual(f.read(), complete)
        self.assertEqual(os.stat(self.archive()).st_mode & 0o777, 0o600)

    def test_restore_after_deletion(self):
        self.write(event("user", 1, "keep me"))
        self.run_cli("index", "-q")
        with open(self.transcript, "rb") as f:
            original = f.read()
        os.remove(self.transcript)
        self.assertIn("1 source transcripts no longer on disk", self.run_cli("stats"))
        self.assertIn("Restored", self.run_cli("restore", SID[:8]))
        with open(self.transcript, "rb") as f:
            self.assertEqual(f.read(), original)
        self.assertIn("still in place", self.run_cli("restore", SID[:8]))
        self.assertIn("[keep] me", self.run_cli("search", "keep"))

    def test_rewritten_transcript_restarts_archive(self):
        self.write(event("user", 1, "long original content"), event("assistant", 2, "more"))
        self.run_cli("index", "-q")
        self.write(event("user", 3, "short"), mode="w")
        self.run_cli("index", "-q")
        with gzip.open(self.archive(), "rb") as f, open(self.transcript, "rb") as g:
            self.assertEqual(f.read(), g.read())

    def test_files_and_git_touches(self):
        tool = {"type": "assistant", "uuid": "t1", "sessionId": SID, "cwd": "/tmp/proj",
                "timestamp": "2026-10-10T10:05:00Z",
                "message": {"content": [
                    {"type": "tool_use", "name": "Write", "input": {"file_path": "/tmp/proj/a.py"}},
                    {"type": "tool_use", "name": "Bash", "input": {
                        "command": "cd /tmp/proj && git commit -qm \"Add the thing\" && git push"}}]}}
        self.write(event("user", 1, "write it"), json.dumps(tool) + "\n")
        out = self.run_cli("card", SID[:8])
        self.assertIn("/tmp/proj/a.py", out)
        self.assertIn("commit, push — Add the thing", out)

    def test_cards_off_by_default(self):
        self.write(event("user", 1, "hello"))
        self.assertIn("Session cards are OFF", self.run_cli("cards-ctl", "status"))


if __name__ == "__main__":
    unittest.main()
