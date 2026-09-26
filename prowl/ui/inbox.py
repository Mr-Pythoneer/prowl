"""A local inbox, so other programs on this Mac can hand Bob a request.

Bob is always on; the on-demand voice assistant (~/voice-assistant) is not.
When that assistant gets something Bob is better at — apps, the system, the
browser, anything needing the cloud brain or the agent — it posts the request
here instead of re-implementing it.

Deliberately small and locked down:
  * binds 127.0.0.1 only — nothing off this machine can reach it
  * every request needs the bearer token in ~/.prowl/inbox_token (0600), so a
    web page or another local user can't drive Bob either (browsers can't
    attach that header cross-origin without a CORS preflight we never answer)
  * requests run through the same Orchestrator as a typed turn, so every
    existing confirmation (destructive skills, escalation) still applies

  POST /task   {"text": "...", "silent": true}  → 202 {"id": "..."}
  GET  /task/<id>                              → {"status", "ok", "speech", "detail"}
  GET  /memory                                 → {"notes": [...], "last_skill": ...}
  GET  /health                                 → {"ok": true, "name": "Bob"}
"""
from __future__ import annotations

import hmac
import json
import queue
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from ..core.config import CONFIG_PATH

TOKEN_PATH = CONFIG_PATH.parent / "inbox_token"
DEFAULT_PORT = 18790
_MAX_BODY = 16 * 1024
_KEEP_TASKS = 50

# run(text, silent) -> (ok, speech, detail)
Runner = Callable[[str, bool], "tuple[bool, str, str]"]


def load_token(path: Path = TOKEN_PATH) -> str:
    """Read the shared token, creating it (0600) on first use."""
    try:
        tok = path.read_text().strip()
        if tok:
            return tok
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    path.write_text(tok + "\n")
    path.chmod(0o600)
    return tok


class Inbox:
    def __init__(self, run: Runner, memory: Callable[[], dict[str, Any]], log,
                 port: int = DEFAULT_PORT, name: str = "Bob", token_path: Path = TOKEN_PATH):
        self.run, self.memory, self.log, self.name = run, memory, log, name
        self.port = port
        self.token = load_token(token_path)
        self.tasks: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._q: queue.Queue[str | None] = queue.Queue()
        self._server: ThreadingHTTPServer | None = None

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> "Inbox":
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), self._handler_class())
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True, name="prowl-inbox").start()
        # One worker: inbox requests run one at a time, in order, like typed turns.
        threading.Thread(target=self._worker, daemon=True, name="prowl-inbox-worker").start()
        self.log.info("inbox listening on 127.0.0.1:%d", self.port)
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self._q.put(None)

    # -- tasks ---------------------------------------------------------------
    def submit(self, text: str, silent: bool) -> str:
        tid = uuid.uuid4().hex[:12]
        with self._lock:
            self.tasks[tid] = {"id": tid, "status": "queued", "text": text, "silent": silent,
                               "created": time.time(), "ok": None, "speech": "", "detail": ""}
            while len(self.tasks) > _KEEP_TASKS:
                self.tasks.popitem(last=False)
        self._q.put(tid)
        return tid

    def get(self, tid: str) -> dict[str, Any] | None:
        with self._lock:
            t = self.tasks.get(tid)
            return dict(t) if t else None

    def _set(self, tid: str, **kw) -> None:
        with self._lock:
            if tid in self.tasks:
                self.tasks[tid].update(kw)

    def _worker(self) -> None:
        while (tid := self._q.get()) is not None:
            t = self.get(tid)
            if t is None:
                continue
            self._set(tid, status="running")
            self.log.info("inbox task %s: %r", tid, t["text"])
            try:
                ok, speech, detail = self.run(t["text"], t["silent"])
                self._set(tid, status="done", ok=ok, speech=speech, detail=detail)
            except Exception as exc:  # noqa: BLE001 - one bad task must not kill the inbox
                self.log.exception("inbox task %s failed", tid)
                self._set(tid, status="error", ok=False, speech="That request failed.", detail=str(exc))

    # -- HTTP ----------------------------------------------------------------
    def _handler_class(self):
        inbox = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ProwlInbox/1"

            def log_message(self, fmt, *args):  # route to Prowl's log, not stderr
                inbox.log.debug("inbox: " + fmt, *args)

            def _send(self, code: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authed(self) -> bool:
                got = self.headers.get("Authorization", "")
                if hmac.compare_digest(got.encode(), f"Bearer {inbox.token}".encode()):
                    return True
                self._send(401, {"error": "unauthorized"})
                return False

            def do_GET(self):
                if not self._authed():
                    return
                path = self.path.split("?", 1)[0].rstrip("/")
                if path == "/health":
                    self._send(200, {"ok": True, "name": inbox.name})
                elif path == "/memory":
                    self._send(200, inbox.memory())
                elif path.startswith("/task/"):
                    t = inbox.get(path[len("/task/"):])
                    self._send(200, t) if t else self._send(404, {"error": "no such task"})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                if not self._authed():
                    return
                if self.path.rstrip("/") != "/task":
                    self._send(404, {"error": "not found"})
                    return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if not 0 < n <= _MAX_BODY:
                    self._send(413 if n > _MAX_BODY else 400, {"error": "bad body size"})
                    return
                try:
                    body = json.loads(self.rfile.read(n))
                    text = str(body.get("text", "")).strip()
                except (json.JSONDecodeError, AttributeError):
                    self._send(400, {"error": "expected JSON {\"text\": ...}"})
                    return
                if not text:
                    self._send(400, {"error": "empty text"})
                    return
                tid = inbox.submit(text, bool(body.get("silent", True)))
                self._send(202, {"id": tid, "status": "queued"})

        return Handler


def is_up(port: int = DEFAULT_PORT, token_path: Path = TOKEN_PATH, timeout: float = 1.0) -> bool:
    """For `prowl doctor`: is the running Bob answering on its inbox?"""
    import urllib.request

    try:
        tok = token_path.read_text().strip()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health",
                                     headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read()).get("ok") is True
    except Exception:  # noqa: BLE001
        return False
