"""Prowl configuration.

Config lives at ``~/.prowl/config.json`` (never in the repo). Missing keys fall
back to DEFAULTS, so a fresh machine works with no config file at all. Call
``Config.load()`` to get a live object; ``.save()`` writes it back.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

PROWL_HOME = Path(os.path.expanduser("~/.prowl"))
CONFIG_PATH = PROWL_HOME / "config.json"
LOG_DIR = PROWL_HOME / "logs"

DEFAULTS: dict[str, Any] = {
    # ---- brain selection ----------------------------------------------------
    # "auto"  -> a cheap cloud model when one is configured and reachable,
    #            falling back to Ollama on any network failure (default)
    # "cloud" -> cloud only; failures surface instead of silently degrading
    # "local" -> Ollama only; never touches the network
    # `offline: true` forces "local" regardless.
    "brain_backend": "auto",

    # ---- cheap cloud model (OpenAI-compatible) ------------------------------
    # Routing and one-line answers are small jobs, so a hosted model is quick
    # and costs fractions of a cent — and keeps ~3 GB of GPU free on a laptop.
    # The key comes from PROWL_CLOUD_API_KEY / DEEPSEEK_API_KEY first, so it
    # need not be written to disk at all.
    "cloud_base_url": "https://api.deepseek.com",
    "cloud_model": "deepseek-chat",
    "cloud_api_key": "",
    "cloud_timeout": 20,

    # ---- local fast model (Ollama), the offline fallback --------------------
    "ollama_url": "http://127.0.0.1:11434",
    "model": "llama3.2:3b",          # fast, instant — the user's chosen model
    "model_timeout": 30,             # seconds for a local generation
    # Ollama loads a model at its MAX context unless told otherwise — for
    # llama3.2:3b that is 131072 tokens and ~17 GB of GPU RAM, which makes a
    # 24 GB Mac unusable while Prowl thinks. Prowl's prompts are small (a
    # system prompt plus one utterance), so a modest window is plenty.
    "num_ctx": 8192,                 # context window handed to Ollama
    "model_keep_alive": "30s",       # free the GPU shortly after each turn

    # ---- escalation ("smart") backend ---------------------------------------
    # How Prowl hands hard/open-ended tasks to a full agent.
    #   "openclaw" -> `openclaw agent -m <task> --json`   (Opus 4.8, can run shell)
    #   "claude"   -> `claude -p <task>`                  (Claude Code, non-interactive)
    #   "off"      -> never escalate; local model only
    "escalation_backend": "claude",
    "escalation_thinking": "medium",  # off|minimal|low|medium|high|xhigh|max
    "escalation_timeout": 120,        # seconds

    # ---- offline mode -------------------------------------------------------
    # When True, Prowl NEVER escalates to the online agent (OpenClaw/Claude) and
    # runs entirely on the local model — no online/metered calls, easy on the
    # wallet. Everything the local model + built-in skills can do still works;
    # only open-ended "have the big agent go do this" tasks are unavailable.
    # Toggle with `prowl offline on|off` or the menu-bar switch.
    "offline": False,

    # ---- voice --------------------------------------------------------------
    "voice_enabled": True,
    "tts_voice": "Samantha",          # macOS `say` voice; "" = system default
    "tts_rate": 190,                  # words per minute
    "stt_locale": "en-US",
    "stt_max_seconds": 12,            # cap on a single dictation

    # ---- interaction --------------------------------------------------------
    # ---- desktop buddy ------------------------------------------------------
    "buddy_enabled": True,           # the floating animated character
    "buddy_greet": True,             # say hello when `prowl serve` starts
    "buddy_mini": False,             # compact mode: character only, no bubble
    "buddy_mini_hotkey": "<cmd>+<shift>+z",   # toggles compact mode

    "hotkey": "<f5>",                # pynput global hotkey to start listening
    # Type instead of talking: same brain, same skills, but the answer is
    # shown rather than spoken — for when the room is quiet or a command is
    # too fiddly to dictate.
    "type_hotkey": "<f4>",
                                 # (F5 is the mic key on Apple keyboards;
                                 #  bare key names are wrapped automatically)
    # What you call him. Used as the spoken wake word and in his own replies.
    # Keep it two syllables or more if you can — a very short word ("Bob") is
    # easier for the recognizer to hear inside ordinary conversation, which
    # means the occasional false wake.
    "assistant_name": "Bob",
    "wake_word": "bob",               # spoken wake word (when always-listening)
    "always_listening": False,        # off by default (privacy); hotkey-driven

    # ---- safety -------------------------------------------------------------
    # Destructive skills (cleanup, delete, shell writes) require confirmation
    # and run as dry-runs unless explicitly applied.
    "confirm_destructive": True,
    "shell_skill_enabled": True,      # allow the guarded free-form shell skill
    "trash_instead_of_delete": True,  # move to Trash (recoverable), never rm -rf

    # ---- cleanup targets (junk categories the cleanup skill may reclaim) -----
    # Each is scanned + sized first; nothing is removed without confirmation.
    "cleanup_categories": [
        "user_caches",        # ~/Library/Caches (app caches, regenerated)
        "user_logs",          # ~/Library/Logs
        "trash",              # empty ~/.Trash
        "xcode_deriveddata",  # ~/Library/Developer/Xcode/DerivedData
        "ios_device_support", # old ~/Library/Developer/Xcode/iOS DeviceSupport
        "simulator_caches",   # ~/Library/Developer/CoreSimulator/Caches
        "npm_cache",          # ~/.npm/_cacache
        "pip_cache",          # ~/Library/Caches/pip
        "homebrew_cache",     # `brew cleanup`
        "ds_store",           # stray .DS_Store files under $HOME
        "pycache",            # __pycache__ dirs under common project roots
    ],
}


class Config:
    """Dict-backed config with attribute access and disk persistence."""

    def __init__(self, data: dict[str, Any] | None = None):
        merged = copy.deepcopy(DEFAULTS)
        if data:
            merged.update(data)
        self._data = merged

    # -- dict-ish access ------------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def __getattr__(self, key: str) -> Any:
        # Only called when normal attribute lookup fails.
        try:
            return self._data[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def escalation_enabled(self) -> bool:
        """True only if an escalation backend is set AND offline mode is off.

        This is the single source of truth for "may Prowl make an online call?"
        Router, Escalator, and the Orchestrator all defer to it.
        """
        return (
            self.get("escalation_backend") not in ("off", "", None)
            and not self.get("offline", False)
        )

    # -- persistence ----------------------------------------------------------
    @staticmethod
    def load_env_file(path: Path | None = None) -> None:
        """Load ``~/.prowl/env`` (KEY=VALUE lines) into the environment.

        A launched .app inherits almost nothing from your shell, so an
        ``export DEEPSEEK_API_KEY=...`` in ~/.zshrc is invisible to Bob even
        though it works fine in a terminal. This file is the GUI-side
        equivalent. Existing environment variables always win, so a terminal
        run can still override it.
        """
        env_path = path or (CONFIG_PATH.parent / "env")
        try:
            text = env_path.read_text()
        except OSError:
            return
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        cls.load_env_file()
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                # A broken config should never brick the assistant.
                data = {}
        return cls(data)

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._data, indent=2, sort_keys=True))


def ensure_home() -> None:
    """Make sure ~/.prowl and its log dir exist."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
