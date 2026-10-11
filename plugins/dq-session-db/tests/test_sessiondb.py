"""End-to-end tests for sessiondb.py: run the CLI against throwaway transcripts.

python3 -m unittest discover -s plugins/dq-session-db/tests
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
        self.env.pop("CLAUDE_CODE_SESSION_ID", None)

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
        self.assertIn(f"cd /tmp/proj && claude --resume {SID}", out)

    def test_upgrade_backs_up_and_newer_schema_is_refused(self):
        import sqlite3
        self.write(event("user", 1, "hello"))
        self.run_cli("index", "-q")
        con = sqlite3.connect(self.db)
        con.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        con.commit()
        con.close()
        self.run_cli("index", "-q")  # upgrades v2 -> current
        backup = f"{self.db}.pre-v{sessiondb.SCHEMA_VERSION}.bak"
        self.assertTrue(os.path.exists(backup))
        self.assertEqual(os.stat(backup).st_mode & 0o777, 0o600)
        con = sqlite3.connect(self.db)
        con.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        con.commit()
        con.close()
        r = subprocess.run([sys.executable, SCRIPT, "search", "hello"], env=self.env,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("newer session-db", r.stderr)
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "99")
        con.close()

    def test_legacy_archive_moves_out_of_claude_folder(self):
        home = os.path.join(self.tmp.name, "home")
        legacy = os.path.join(home, ".claude", "session-db")
        os.makedirs(legacy)
        self.write(event("user", 1, "legacy narwhal"))
        old_env = {**self.env, "SESSION_DB": os.path.join(legacy, "sessions.db")}
        subprocess.run([sys.executable, SCRIPT, "index", "-q"], env=old_env, check=True)
        with open(os.path.join(legacy, "config.json"), "w") as f:
            f.write("{}")
        env = {k: v for k, v in self.env.items() if k != "SESSION_DB"}
        env.update(HOME=home, XDG_DATA_HOME="")
        r = subprocess.run([sys.executable, SCRIPT, "search", "narwhal"], env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("legacy [narwhal]", r.stdout)
        self.assertIn("moved the archive", r.stderr)
        new = os.path.join(home, ".local", "share", "session-db")
        self.assertTrue(os.path.exists(os.path.join(new, "sessions.db")))
        self.assertTrue(os.path.exists(os.path.join(new, "config.json")))
        self.assertTrue(os.path.isdir(legacy + ".migrated"))
        self.assertFalse(os.path.exists(legacy))
        self.assertEqual(os.stat(new).st_mode & 0o777, 0o700)

    def add_session(self, sid, *lines):
        path = os.path.join(self.proj, sid + ".jsonl")
        with open(path, "w") as f:
            f.write("".join(l.replace(SID, sid) for l in lines))
        return path

    def no_idle_wait(self):
        os.makedirs(os.path.dirname(self.db), exist_ok=True)
        with open(os.path.join(os.path.dirname(self.db), "config.json"), "w") as f:
            json.dump({"cleanup": {"idle_hours": 0}}, f)

    def test_current_session_left_out_of_search(self):
        self.write(event("user", 1, "unique quokka"))
        env = {**self.env, "CLAUDE_CODE_SESSION_ID": SID}
        r = subprocess.run([sys.executable, SCRIPT, "search", "quokka"], env=env,
                           capture_output=True, text=True)
        self.assertIn("No matches", r.stdout)
        r = subprocess.run([sys.executable, SCRIPT, "search", "quokka", "--include-current"],
                           env=env, capture_output=True, text=True)
        self.assertIn("[quokka]", r.stdout)

    def test_cleanup_finds_lookups_and_tiny_but_not_real_work(self):
        self.no_idle_wait()
        lookup = "11111111-0000-0000-0000-000000000000"
        work = "22222222-0000-0000-0000-000000000000"
        call = {"type": "assistant", "uuid": "x1", "sessionId": SID, "cwd": "/tmp/proj",
                "timestamp": "2026-10-01T10:00:00Z", "message": {"content": [
                    {"type": "tool_use", "name": "Bash",
                     "input": {"command": "python3 sessiondb.py search pelican"}}]}}
        for i in range(5):
            self.add_session(lookup, *[event("user", i, f"find pelican {i}")])
        self.add_session(lookup, *[event("user", i, f"find pelican {i}") for i in range(5)],
                         json.dumps(call) + "\n")
        write = dict(call, uuid="x2", message={"content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": "/tmp/proj/x.py"}}]})
        self.add_session(work, event("user", 1, "build it"), json.dumps(write) + "\n")
        self.write(event("user", 1, "hi"))  # tiny
        out = self.run_cli("cleanup")
        self.assertIn(lookup, out)
        self.assertIn("lookup", out)
        self.assertIn(SID, out)
        self.assertIn("tiny", out)
        self.assertNotIn(work, out)
        self.assertIn("Nothing changed", out)

    def test_lookup_sessions_left_out_of_search(self):
        lookup = "11111111-0000-0000-0000-000000000000"
        call = {"type": "assistant", "uuid": "x1", "sessionId": SID, "cwd": "/tmp/proj",
                "timestamp": "2026-10-01T10:00:00Z", "message": {"content": [
                    {"type": "tool_use", "name": "Skill", "input": {"skill": "dq-session-db:sessions"}}]}}
        self.add_session(lookup, *[event("user", i, f"find the heron heron project {i}") for i in range(4)],
                         json.dumps(call) + "\n")
        self.write(event("user", 1, "design the heron pipeline"))
        out = self.run_cli("search", "heron", "--by-session")
        self.assertIn(SID, out)
        self.assertNotIn(lookup, out)
        self.assertIn("1 lookup session(s)", out)
        out = self.run_cli("search", "heron", "--by-session", "--include-lookups")
        lines = [l for l in out.splitlines() if l.strip()]
        self.assertTrue(lines[0].startswith(SID), out)
        self.assertIn("[lookup", lines[1])

    def test_hide_and_unhide(self):
        self.no_idle_wait()
        self.write(event("user", 1, "hidden marmot"))
        self.run_cli("cleanup", "--hide")
        self.assertIn("No matches", self.run_cli("search", "marmot"))
        self.assertIn("[marmot]", self.run_cli("search", "marmot", "--all"))
        self.run_cli("unhide", SID[:8])
        self.assertIn("[marmot]", self.run_cli("search", "marmot"))

    def test_remove_needs_yes_backs_up_and_stays_removed(self):
        self.no_idle_wait()
        self.write(event("user", 1, "doomed walrus"))
        self.run_cli("index", "-q")
        self.assertIn("Re-run with --yes", self.run_cli("cleanup", "--remove"))
        self.assertIn("[walrus]", self.run_cli("search", "walrus"))
        out = self.run_cli("cleanup", "--remove", "--yes")
        self.assertIn("Removed 1 session(s)", out)
        backup = out.split("Backup: ")[1].strip()
        self.assertTrue(os.path.exists(os.path.join(backup, "sessions.db")))
        self.assertTrue(any(f.endswith(".jsonl.gz") for f in os.listdir(backup)))
        self.run_cli("reindex", "--full", "-q")
        self.assertIn("No matches", self.run_cli("search", "walrus", "--all"))

    def test_cards_off_by_default(self):
        self.write(event("user", 1, "hello"))
        self.assertIn("Session cards are OFF", self.run_cli("cards-ctl", "status"))


if __name__ == "__main__":
    unittest.main()
