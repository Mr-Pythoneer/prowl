"""Inbox tests. Standard library only (Prowl has no test deps):
    .venv/bin/python -m unittest discover -s tests
"""
import json
import logging
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from prowl.ui.inbox import Inbox, is_up

PORT = 18799


def call(method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class InboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.token_path = Path(self.tmp.name) / "inbox_token"
        self.seen = []

        def run(text, silent):
            self.seen.append((text, silent))
            if text == "boom":
                raise RuntimeError("kaboom")
            return True, f"did {text}", "details"

        self.inbox = Inbox(run, lambda: {"notes": ["parked on 3"], "last_skill": None},
                           logging.getLogger("t"), port=PORT, token_path=self.token_path).start()
        self.tok = self.inbox.token

    def tearDown(self):
        self.inbox.stop()
        self.tmp.cleanup()

    def wait(self, tid):
        for _ in range(50):
            code, t = call("GET", f"/task/{tid}", self.tok)
            if t.get("status") in ("done", "error"):
                return t
            time.sleep(0.05)
        self.fail("task never finished")

    def test_token_file_is_private(self):
        self.assertEqual(self.token_path.stat().st_mode & 0o777, 0o600)
        self.assertGreater(len(self.tok), 30)

    def test_rejects_without_or_with_wrong_token(self):
        self.assertEqual(call("GET", "/health")[0], 401)
        self.assertEqual(call("GET", "/health", "nope")[0], 401)
        self.assertEqual(call("POST", "/task", "nope", {"text": "open Safari"})[0], 401)
        self.assertEqual(self.seen, [])

    def test_task_round_trip(self):
        code, r = call("POST", "/task", self.tok, {"text": "open Safari", "silent": True})
        self.assertEqual(code, 202)
        t = self.wait(r["id"])
        self.assertEqual((t["status"], t["ok"], t["speech"]), ("done", True, "did open Safari"))
        self.assertEqual(self.seen, [("open Safari", True)])

    def test_failing_task_reports_error_and_inbox_survives(self):
        _, r = call("POST", "/task", self.tok, {"text": "boom"})
        self.assertEqual(self.wait(r["id"])["status"], "error")
        _, r = call("POST", "/task", self.tok, {"text": "still alive"})
        self.assertEqual(self.wait(r["id"])["status"], "done")

    def test_bad_bodies(self):
        self.assertEqual(call("POST", "/task", self.tok, {"text": "  "})[0], 400)
        self.assertEqual(call("POST", "/task", self.tok, {"text": "x" * 20000})[0], 413)
        self.assertEqual(call("GET", "/task/unknown", self.tok)[0], 404)

    def test_memory_and_health(self):
        self.assertEqual(call("GET", "/memory", self.tok)[1]["notes"], ["parked on 3"])
        self.assertEqual(call("GET", "/health", self.tok)[1], {"ok": True, "name": "Bob"})
        self.assertTrue(is_up(PORT, self.token_path))


if __name__ == "__main__":
    unittest.main()
