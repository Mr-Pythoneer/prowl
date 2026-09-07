"""The fast local model (llama3.2:3b via Ollama).

Talks to a local Ollama server over HTTP using only the standard library, so the
core has no third-party dependencies. Used for two things:

* instant conversational replies, and
* structured intent classification (``chat`` / ``skill`` / ``escalate``).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from ..core.config import Config


class LocalModelError(RuntimeError):
    pass


class LocalBrain:
    def __init__(self, config: Config):
        self.cfg = config
        self.base = config.ollama_url.rstrip("/")
        self.model = config.model
        self.timeout = config.model_timeout

    # -- low-level ------------------------------------------------------------
    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise LocalModelError(
                f"Could not reach Ollama at {self.base} ({exc}). Is `ollama serve` running?"
            ) from exc

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base}/api/tags", timeout=3) as resp:
                tags = json.loads(resp.read().decode("utf-8"))
            names = {m.get("name", "") for m in tags.get("models", [])}
            # Accept an exact match or the same model with any tag.
            base = self.model.split(":")[0]
            return any(n == self.model or n.split(":")[0] == base for n in names)
        except Exception:
            return False

    # -- high-level -----------------------------------------------------------
    def chat(self, system: str, user: str, *, json_mode: bool = False,
             temperature: float = 0.2) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {
                "temperature": temperature,
                # Without num_ctx, Ollama allocates the model's maximum context
                # (131k for llama3.2 => ~17 GB) and the Mac grinds to a halt.
                "num_ctx": int(self.cfg.get("num_ctx", 8192)),
            },
            # Release the GPU soon after answering instead of holding it for
            # the default 5 minutes.
            "keep_alive": self.cfg.get("model_keep_alive", "30s"),
        }
        if json_mode:
            payload["format"] = "json"
        out = self._post("/api/chat", payload)
        return (out.get("message") or {}).get("content", "").strip()

    def reply(self, user: str, *, persona: str = "") -> str:
        """A quick, spoken-length conversational answer."""
        system = (
            f"You are {self.cfg.get('assistant_name', 'Bob')}, a concise voice "
            "assistant on the user's Mac. "
            "Answer in one or two short spoken sentences. No markdown, no lists. "
            + persona
        )
        return self.chat(system, user, temperature=0.4)
