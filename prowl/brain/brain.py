"""Brain selection: a cheap cloud model when online, the local one when not.

Prowl's thinking is small — classify an utterance, or answer in a sentence — so
the cloud is the better default when it's reachable: faster, sharper, costs
fractions of a cent, and leaves the laptop's GPU alone. But a desktop assistant
has to keep working on a plane, so :class:`Brain` transparently falls back to
Ollama whenever the cloud call fails for an *environmental* reason (no wifi,
timeout, rate limit, no credit).

Selection is controlled by ``brain_backend``:

* ``"auto"``  — cloud when it's configured and reachable, else local (default)
* ``"cloud"`` — cloud only; errors surface rather than silently degrading
* ``"local"`` — local only; never touches the network

``offline: true`` forces local regardless, so the existing offline switch keeps
its meaning: no online calls, at all, for any reason.
"""
from __future__ import annotations

from ..core.config import Config
from .cloud import CloudBrain, CloudModelError
from .local import LocalBrain, LocalModelError


class BrainError(RuntimeError):
    pass


class Brain:
    """Cloud-first, local-fallback. Same interface as either one alone."""

    def __init__(self, config: Config):
        self.cfg = config
        self.local = LocalBrain(config)
        self.cloud = CloudBrain(config)
        # Which backend answered last — for logging and `prowl doctor`.
        self.last_used: str = "none"

    # -- policy ---------------------------------------------------------------
    def _mode(self) -> str:
        if self.cfg.get("offline", False):
            return "local"
        mode = str(self.cfg.get("brain_backend", "auto")).strip().lower()
        return mode if mode in ("auto", "cloud", "local") else "auto"

    def _use_cloud(self) -> bool:
        mode = self._mode()
        if mode == "local":
            return False
        if mode == "cloud":
            return True
        return self.cloud.configured()

    # -- interface ------------------------------------------------------------
    def available(self) -> bool:
        if self._use_cloud() and self.cloud.available():
            return True
        return self.local.available()

    def chat(self, system: str, user: str, *, json_mode: bool = False,
             temperature: float = 0.2) -> str:
        return self._run(
            lambda b: b.chat(system, user, json_mode=json_mode, temperature=temperature)
        )

    def reply(self, user: str, *, persona: str = "") -> str:
        return self._run(lambda b: b.reply(user, persona=persona))

    def _run(self, call):
        """Try the chosen backend; fall back to local when the cloud can't answer."""
        if self._use_cloud():
            try:
                out = call(self.cloud)
                self.last_used = "cloud"
                return out
            except CloudModelError as exc:
                if self._mode() == "cloud":
                    # Explicitly cloud-only: don't paper over a real failure.
                    raise BrainError(str(exc)) from exc
                # Otherwise this is exactly the no-wifi case the local model
                # exists for — fall through quietly.
                self.last_error = str(exc)

        try:
            out = call(self.local)
            self.last_used = "local"
            return out
        except LocalModelError as exc:
            self.last_used = "none"
            raise BrainError(str(exc)) from exc
