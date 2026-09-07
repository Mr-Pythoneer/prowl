"""A cheap cloud model — the everyday brain when there's a network.

Prowl's model job is small: classify one short utterance, or answer a one-line
question. A hosted model does that faster and better than a 3B running locally,
costs a fraction of a cent per request at these prompt sizes, and — the real
win on a laptop — keeps several gigabytes of GPU memory free.

The API is OpenAI-compatible, so this works unchanged against DeepSeek (the
default), OpenAI, Groq, Together, or anything else speaking that shape: point
``cloud_base_url`` and ``cloud_model`` wherever you like.

Stdlib only, matching :mod:`prowl.brain.local`, and it exposes the same
``chat`` / ``reply`` / ``available`` interface so the two are interchangeable.

The API key is read from the ``PROWL_CLOUD_API_KEY`` or ``DEEPSEEK_API_KEY``
environment variable first, then ``cloud_api_key`` in the config file. Keeping
it in the environment means the key never has to live in a file at all.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from ..core.config import Config


class CloudModelError(RuntimeError):
    pass


# Env vars checked in order; the first non-empty one wins.
_KEY_ENV = ("PROWL_CLOUD_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")


class CloudBrain:
    """A hosted chat model behind the same interface as :class:`LocalBrain`."""

    def __init__(self, config: Config):
        self.cfg = config
        self.base = str(config.get("cloud_base_url", "https://api.deepseek.com")).rstrip("/")
        self.model = str(config.get("cloud_model", "deepseek-chat"))
        self.timeout = int(config.get("cloud_timeout", 20))

    # -- key ------------------------------------------------------------------
    @property
    def api_key(self) -> str:
        for env in _KEY_ENV:
            val = os.environ.get(env, "").strip()
            if val:
                return val
        return str(self.cfg.get("cloud_api_key", "") or "").strip()

    def configured(self) -> bool:
        """True if a key is present. Does not touch the network."""
        return bool(self.api_key)

    # -- low-level ------------------------------------------------------------
    def _post(self, payload: dict) -> dict:
        key = self.api_key
        if not key:
            raise CloudModelError(
                "No cloud API key. Set DEEPSEEK_API_KEY in your environment, "
                "or `prowl config set cloud_api_key <key>`."
            )
        req = urllib.request.Request(
            f"{self.base}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001 - error reporting must not raise
                pass
            if exc.code in (401, 403):
                raise CloudModelError(
                    "The cloud API rejected the key (check DEEPSEEK_API_KEY)."
                ) from exc
            if exc.code == 402:
                raise CloudModelError(
                    "The cloud account is out of credit."
                ) from exc
            if exc.code == 429:
                raise CloudModelError("Rate-limited by the cloud API.") from exc
            raise CloudModelError(f"Cloud API error {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            # No wifi, DNS failure, timeout — the caller falls back to local.
            raise CloudModelError(f"Could not reach {self.base} ({exc.reason}).") from exc
        except (TimeoutError, OSError) as exc:
            raise CloudModelError(f"Could not reach {self.base} ({exc}).") from exc

    # -- high-level -----------------------------------------------------------
    def available(self) -> bool:
        """Cheapest possible liveness check: a 1-token completion."""
        if not self.configured():
            return False
        try:
            self._post({
                "model": self.model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
                "stream": False,
            })
            return True
        except CloudModelError:
            return False

    def chat(self, system: str, user: str, *, json_mode: bool = False,
             temperature: float = 0.2) -> str:
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        out = self._post(payload)
        try:
            return (out["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise CloudModelError(f"Unexpected response shape: {str(out)[:200]}") from exc

    def reply(self, user: str, *, persona: str = "") -> str:
        """A quick, spoken-length conversational answer."""
        system = (
            "You are Prowl, a concise voice assistant on the user's Mac. "
            "Answer in one or two short spoken sentences. No markdown, no lists. "
            + persona
        )
        return self.chat(system, user, temperature=0.4)
