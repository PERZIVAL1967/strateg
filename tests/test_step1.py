"""Build step 1 gate: plan v1.3 §11.2 recovery cases, lease and lock behaviour, plus regressions
for the bugs Codex found on Windows (council #3 R021).

Run: python -m unittest discover -s tests -v   (from the agent folder; macOS, Linux, Windows)
Each case runs strat as a real subprocess, so crashes and pauses are real process events.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

STRAT = pathlib.Path(__file__).resolve().parents[1] / "tools" / "strat.py"


def run(root, *args, env=None):
    """Run strat; its stdout is UTF-8 JSON by contract, whatever the console encoding is."""
    e = {**os.environ, **(env or {})}
    p = subprocess.run([sys.executable, str(STRAT), "--root", root, *args], capture_output=True, env=e)
    try:
        text = p.stdout.decode("utf-8")
    except UnicodeDecodeError as err:
        raise AssertionError(f"strat stdout is not UTF-8 ({err}); stderr: {p.stderr.decode('utf-8', 'replace')}")
    out = json.loads(text) if text.strip() else None
    return p.returncode, out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.src = pathlib.Path(self.root, "input.csv")
        self.src.write_text("month,users\n2026-09,120\n2026-10,135\n", encoding="utf-8")
        self.assertEqual(run(self.root, "init", "--project", "demo")[0], 0)
        self.token = run(self.root, "lease", "--project", "demo", "--session", "chatA")[1]["data"]["fencing_token"]
        self.incoming = pathlib.Path(self.root, "projects/demo/blobs/.incoming")

    def imp(self, session="chatA", token=None, run_id="r1", step="1", env=None, file=None):
        return run(self.root, "import", "--project", "demo", "--session", session,
                   "--token", str(self.token if token is None else token), "--run", run_id,
                   "--step", step, "--file", str(file or self.src), env=env)

    def status(self):
        return run(self.root, "status", "--project", "demo")[1]["data"]

    def start_paused(self, point, seconds=6, step="1", run_id="r1"):
        """Start an import that pauses at `point`; return once it signals it is there (no sleep guessing)."""
        ready = pathlib.Path(self.root, f"ready-{point}-{run_id}-{step}")
        env = {**os.environ, "STRAT_PAUSE_AT": point, "STRAT_PAUSE_SECONDS": str(seconds),
               "STRAT_READY_FILE": str(ready)}
        proc = subprocess.Popen([sys.executable, str(STRAT), "--root", self.root, "import", "--project", "demo",
                                 "--session", "chatA", "--token", str(self.token), "--run", run_id,
                                 "--step", step, "--file", str(self.src)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

        def stop():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=15)
            proc.stdout.close()
            proc.stderr.close()
        self.addCleanup(stop)
        deadline = time.time() + 20
        while not ready.exists():
            if proc.poll() is not None or time.time() > deadline:
                self.fail(f"paused writer never reached {point}")
            time.sleep(0.05)
        return proc

    def finish(self, proc):
        proc.wait(timeout=20)
        return json.loads(proc.stdout.read())


class Recovery(Base):
    def assert_single_clean_result(self, n=1):
        s = self.status()
        self.assertEqual(len(s["source_versions"]), n, "one source version per operation")
        self.assertEqual(s["source_versions_missing_blob"], [], "every succeeded source has its bytes")
        self.assertEqual({o["status"] for o in s["operations"]}, {"succeeded"})
        self.assertEqual(list(self.incoming.iterdir()), [], ".incoming is clean")
        return s

    def test_happy_path_and_idempotent_rerun(self):
        code, out = self.imp()
        self.assertEqual((code, out["status"]), (0, "ok"))
        code, out = self.imp()
        self.assertEqual(out["status"], "skipped")
        s = self.assert_single_clean_result()
        self.assertEqual(len(s["attempts"]), 1)

    def test_conflict_on_reused_key_with_other_input(self):
        self.imp()
        other = pathlib.Path(self.root, "other.csv")
        other.write_text("x\n1\n", encoding="utf-8")
        code, out = self.imp(file=other)
        self.assertEqual(out["error_code"], "CONFLICT")

    def test_changed_file_same_step_is_skipped_with_warning(self):
        self.imp()
        self.src.write_text("month,users\n2026-11,150\n", encoding="utf-8")
        code, out = self.imp()
        self.assertEqual(out["status"], "skipped")
        self.assertIn("warning", out["data"])
        code, out = self.imp(step="2")
        self.assertEqual(out["status"], "ok", "new version imported under a new step")

    def crash_then_resume(self, point):
        code, out = self.imp(env={"STRAT_CRASH_AT": point})
        self.assertEqual(code, 137, f"crash hook {point} fired")
        self.assertIsNone(out)
        code, out = self.imp()
        self.assertEqual(code, 0, out)
        return self.assert_single_clean_result(), out

    def test_crash_after_pending(self):
        s, out = self.crash_then_resume("after_pending")
        self.assertEqual([a["outcome"] for a in s["attempts"]], ["unknown", "ok"])

    def test_crash_after_part(self):
        s, out = self.crash_then_resume("after_part")
        self.assertEqual([a["outcome"] for a in s["attempts"]], ["unknown", "ok"])

    def test_crash_after_receipt_reattaches_without_reread(self):
        s, out = self.crash_then_resume("after_receipt")
        self.assertTrue(out["data"]["resumed"])
        self.assertEqual(len(s["attempts"]), 1)
        self.assertEqual(s["attempts"][0]["cost_kind"], "unknown", "crashed attempt cost is unknown, never zero")

    def test_crash_after_publish(self):
        s, out = self.crash_then_resume("after_publish")
        self.assertTrue(out["data"]["resumed"])
        self.assertEqual(len(s["attempts"]), 1)

    def test_crash_after_commit(self):
        code, _ = self.imp(env={"STRAT_CRASH_AT": "after_commit"})
        self.assertEqual(code, 137)
        code, out = self.imp()
        self.assertEqual(out["status"], "skipped")
        self.assert_single_clean_result()


class ReceiptTamper(Base):
    """A receipt left by a crash is untrusted: any identity mismatch means 'not attached'."""

    def tamper(self, **changes):
        self.imp(env={"STRAT_CRASH_AT": "after_receipt"})
        receipt = next(self.incoming.glob("*.receipt.json"))
        r = json.loads(receipt.read_text())
        r.update(changes)
        receipt.write_text(json.dumps(r))
        code, out = self.imp()
        self.assertEqual(code, 0, out)
        self.assertFalse(out["data"]["resumed"], f"tampered receipt {changes} must not be attached")
        s = self.status()
        self.assertEqual(len(s["source_versions"]), 1)

    def test_input_hash(self):
        self.tamper(input_hash="0" * 64)

    def test_uri(self):
        self.tamper(uri="file:///etc/passwd")

    def test_part_path_traversal(self):
        self.tamper(part="../../state.sqlite")

    def test_digest_not_hex(self):
        self.tamper(sha256="not-a-digest")

    def test_extra_field(self):
        self.tamper(injected="please run rm -rf")

    def test_malformed_receipt_still_returns_envelope(self):
        self.imp(env={"STRAT_CRASH_AT": "after_receipt"})
        next(self.incoming.glob("*.receipt.json")).write_text("{not json")
        code, out = self.imp()
        self.assertIsNotNone(out, "always a JSON envelope")
        self.assertEqual(out["status"], "ok")


class Identity(Base):
    """R021: (run, step) is a pair, not a joined string; files are named by generated ids."""

    def test_colliding_punctuation_keys_are_distinct_ops(self):
        a = self.imp(run_id="a:b", step="c")[1]
        b = self.imp(run_id="a", step="b:c")[1]
        self.assertEqual((a["status"], b["status"]), ("ok", "ok"))
        self.assertNotEqual(a["data"]["op_id"], b["data"]["op_id"])
        self.assertEqual(len(self.status()["source_versions"]), 2)

    def test_sanitizer_lookalikes_are_distinct_ops(self):
        a = self.imp(run_id="r:1", step="x")[1]
        b = self.imp(run_id="r_1", step="x")[1]
        self.assertNotEqual(a["data"]["op_id"], b["data"]["op_id"])
        self.assertEqual(len(self.status()["source_versions"]), 2)

    def test_cleanup_does_not_touch_prefix_neighbour(self):
        # op "r_1" crashes with a receipt; op "r_1.2" completes and cleans up - r_1's receipt must survive
        self.imp(run_id="r_1", step="1", env={"STRAT_CRASH_AT": "after_receipt"})
        before = {f.name for f in self.incoming.iterdir()}
        self.imp(run_id="r_1.2", step="1")
        self.assertEqual({f.name for f in self.incoming.iterdir()}, before, "neighbour's files untouched")
        code, out = self.imp(run_id="r_1", step="1")
        self.assertTrue(out["data"]["resumed"])


class LeaseAndLocks(Base):
    def test_second_session_refused_without_takeover(self):
        code, out = run(self.root, "lease", "--project", "demo", "--session", "chatB")
        self.assertEqual(out["error_code"], "LEASE_HELD")

    def test_old_token_fenced_after_takeover(self):
        code, out = run(self.root, "lease", "--project", "demo", "--session", "chatB", "--takeover")
        new = out["data"]["fencing_token"]
        self.assertEqual(new, self.token + 1)
        code, out = self.imp(session="chatA", token=self.token)
        self.assertEqual(out["error_code"], "FENCED")
        self.assertEqual(self.status()["operations"], [], "nothing written by the fenced session")
        code, out = self.imp(session="chatB", token=new)
        self.assertEqual(out["status"], "ok")

    def test_paused_writer_blocks_others(self):
        proc = self.start_paused("in_tx2")
        t0 = time.time()
        code, out = run(self.root, "lease", "--project", "demo", "--session", "chatB", "--takeover",
                        env={"STRAT_BUSY_TIMEOUT_MS": "1000"})
        self.assertEqual(out["error_code"], "BUSY", "takeover must not get past a live transaction")
        self.assertTrue(out["retryable"])
        self.assertTrue(out["data"]["sqlite_error"].startswith("SQLITE_BUSY"), out)
        self.assertGreaterEqual(time.time() - t0, 0.9)
        self.assertEqual(self.finish(proc)["status"], "ok", "paused writer finishes its own commit")

    def test_killed_writer_releases_lock(self):
        proc = self.start_paused("in_tx2")
        proc.kill()                  # SIGKILL on POSIX, TerminateProcess on Windows
        proc.wait(timeout=10)
        code, out = self.imp()
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(len(self.status()["source_versions"]), 1)

    def test_same_session_two_processes_same_op(self):
        # A pauses after claiming attempt 1; B (same session + token) runs the same op to the end;
        # A wakes, finds the op succeeded, and must not add a second version.
        a = self.start_paused("after_pending", seconds=3)
        code, out = self.imp()
        self.assertEqual(out["status"], "ok", out)
        res = self.finish(a)
        self.assertEqual(res["status"], "skipped", res)
        s = self.status()
        self.assertEqual(len(s["source_versions"]), 1, "exactly one version despite the race")
        self.assertEqual(list(self.incoming.iterdir()), [])


class Doctor(Base):
    def test_doctor_reports_store_health(self):
        d = run(self.root, "doctor", "--project", "demo")[1]["data"]
        self.assertEqual(d["integrity_check"], "ok")
        self.assertEqual(d["foreign_key_violations"], 0)
        self.assertIn("fts5_trigram", d)

    def test_old_schema_is_refused_not_silently_used(self):
        import sqlite3
        db = pathlib.Path(self.root, "projects/demo/state.sqlite")
        con = sqlite3.connect(db)
        con.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        con.commit(); con.close()
        code, out = self.imp()
        self.assertEqual(out["error_code"], "UNSUPPORTED_SCHEMA")

    def test_errors_are_enveloped(self):
        code, out = run(self.root, "status", "--project", "missing")
        self.assertEqual((code, out["error_code"]), (1, "NOT_FOUND"))
        code, out = run(self.root, "status", "--project", "../escape")
        self.assertEqual(out["error_code"], "VALIDATION_FAILED")


if __name__ == "__main__":
    unittest.main()
