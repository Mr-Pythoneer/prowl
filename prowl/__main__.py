"""Prowl command-line entry point.

    prowl "what's my ip"          one-shot: route + act, print (and speak) the reply
    prowl listen                  capture one voice turn, then act
    prowl serve                   run the always-on menu-bar app
    prowl clean [--apply]         reclaim disk space (dry-run unless --apply)
    prowl offline [on|off]        local-model-only mode (no online agent calls)
    prowl doctor                  check the environment (Ollama, model, OpenClaw, voice)
    prowl config [get|set k v]    read/update ~/.prowl/config.json

The one-shot form is the workhorse and needs nothing but the standard library
plus Ollama; the GUI/voice forms lazy-import their extra pieces.
"""
from __future__ import annotations

import subprocess
import sys

from .core.config import Config, CONFIG_PATH, ensure_home
from .core.context import Context
from .core.logs import get_logger

_SUBCOMMANDS = {"listen", "serve", "clean", "offline", "doctor", "config",
                "help", "--help", "-h"}


# --- shared front-end pieces ------------------------------------------------
def _speak_fn(cfg: Config, aloud: bool):
    def speak(text: str) -> None:
        if not text:
            return
        print(f"🐾 {text}")
        if aloud:
            try:
                from .voice.tts import speak as tts_speak  # canonical impl
                tts_speak(text, cfg)
            except Exception:
                # Fallback: macOS `say` directly, so voice works even if the
                # voice module is missing.
                try:
                    args = ["say"]
                    if cfg.tts_voice:
                        args += ["-v", cfg.tts_voice]
                    args += ["-r", str(cfg.tts_rate), text]
                    subprocess.run(args, timeout=60)
                except Exception:
                    pass
    return speak


def _confirm_fn(assume_yes: bool):
    def confirm(question: str) -> bool:
        if assume_yes:
            print(f"🐾 {question} [auto-yes]")
            return True
        try:
            ans = input(f"🐾 {question} [y/N] ").strip().lower()
        except EOFError:
            return False
        return ans in ("y", "yes")
    return confirm


def _make_context(cfg: Config, *, aloud: bool, assume_yes: bool, dry_run: bool) -> Context:
    ensure_home()
    return Context(
        config=cfg,
        log=get_logger(),
        speak=_speak_fn(cfg, aloud),
        confirm=_confirm_fn(assume_yes),
        dry_run=dry_run,
    )


# --- subcommands ------------------------------------------------------------
def cmd_oneshot(utterance: str, *, speak_aloud: bool, assume_yes: bool) -> int:
    from .executor import Orchestrator

    cfg = Config.load()
    ctx = _make_context(cfg, aloud=speak_aloud and cfg.voice_enabled,
                        assume_yes=assume_yes, dry_run=False)
    orch = Orchestrator(cfg)
    result = orch.handle(utterance, ctx)
    if result.detail:
        print(result.detail)
    return 0 if result.ok else 1


def cmd_listen(assume_yes: bool) -> int:
    from .executor import Orchestrator
    from .voice.stt import listen_once

    cfg = Config.load()
    ctx = _make_context(cfg, aloud=cfg.voice_enabled, assume_yes=assume_yes, dry_run=False)
    print("🎙  Listening… (speak now)")
    text = listen_once(cfg)
    if not text:
        print("Didn't catch anything.")
        return 1
    print(f"Heard: {text}")
    orch = Orchestrator(cfg)
    result = orch.handle(text, ctx)
    if result.detail:
        print(result.detail)
    return 0 if result.ok else 1


def cmd_serve() -> int:
    from .ui.menubar import run_menubar

    run_menubar(Config.load())
    return 0


def cmd_clean(argv: list[str]) -> int:
    from .executor import Executor

    cfg = Config.load()
    apply = "--apply" in argv
    category = "all"
    if "--category" in argv:
        i = argv.index("--category")
        if i + 1 < len(argv):
            category = argv[i + 1]
    ctx = _make_context(cfg, aloud=False, assume_yes="--yes" in argv, dry_run=not apply)
    from . import skills as skills_pkg
    skills_pkg.load_all()
    result = Executor(cfg).run_skill("cleanup", {"category": category, "apply": apply}, ctx)
    print(f"🐾 {result.speech}")
    if result.detail:
        print(result.detail)
    return 0 if result.ok else 1


def cmd_offline(argv: list[str]) -> int:
    cfg = Config.load()
    if not argv or argv[0] in ("status", "get"):
        state = "ON — local model only, no online calls" if cfg.get("offline") \
            else "OFF — hard tasks may escalate to the online agent"
        print(f"🐾 Offline mode is {state}")
        return 0
    val = argv[0].lower()
    if val in ("on", "true", "1", "yes", "enable"):
        cfg.set("offline", True)
        cfg.save()
        print("🐾 Offline mode ON — running entirely on the local model. No online calls.")
        return 0
    if val in ("off", "false", "0", "no", "disable"):
        cfg.set("offline", False)
        cfg.save()
        print("🐾 Offline mode OFF — open-ended tasks may escalate to the online agent.")
        return 0
    print("usage: prowl offline [on|off|status]")
    return 1


def cmd_doctor() -> int:
    from shutil import which

    from .brain.local import LocalBrain

    cfg = Config.load()
    print("Prowl doctor\n" + "=" * 40)
    ok = True

    offline = bool(cfg.get("offline"))
    print(f"•  Mode: {'OFFLINE (local model only)' if offline else 'online (escalation enabled)'}")

    lb = LocalBrain(cfg)
    if lb.available():
        print(f"✅ Ollama reachable, model '{cfg.model}' present")
    else:
        ok = False
        print(f"❌ Ollama or model '{cfg.model}' missing — run `ollama pull {cfg.model}`")

    backend = cfg.escalation_backend
    if offline:
        print("•  Escalation: OFF (offline mode — local model only)")
    elif backend == "off":
        print("•  Escalation disabled (local only)")
    elif backend == "openclaw":
        print("✅ Escalation: openclaw" if which("openclaw") else "❌ openclaw not on PATH")
        ok = ok and bool(which("openclaw"))
    elif backend == "claude":
        print("✅ Escalation: claude" if which("claude") else "❌ claude not on PATH")
        ok = ok and bool(which("claude"))

    print("✅ `say` (TTS) available" if which("say") else "❌ `say` missing")

    from pathlib import Path
    helper = Path(__file__).parent / "helpers" / "prowl-listen"
    print("✅ Voice STT helper built" if helper.exists()
          else "•  Voice STT helper not built yet — run scripts/build_stt.sh")

    print(f"•  Config: {CONFIG_PATH} ({'exists' if CONFIG_PATH.exists() else 'defaults'})")
    print("=" * 40)
    print("All good 🐾" if ok else "Some checks failed — see above.")
    return 0 if ok else 1


def cmd_config(argv: list[str]) -> int:
    cfg = Config.load()
    if not argv or argv[0] == "get":
        import json
        key = argv[1] if len(argv) > 1 else None
        if key:
            print(cfg.get(key))
        else:
            print(json.dumps(cfg.as_dict(), indent=2, sort_keys=True))
        return 0
    if argv[0] == "set" and len(argv) >= 3:
        key, raw = argv[1], argv[2]
        # Best-effort typing: json-parse, else keep string.
        import json
        try:
            val = json.loads(raw)
        except json.JSONDecodeError:
            val = raw
        cfg.set(key, val)
        cfg.save()
        print(f"set {key} = {val!r}  ->  {CONFIG_PATH}")
        return 0
    print("usage: prowl config [get [key] | set <key> <value>]")
    return 1


HELP = __doc__


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("help", "--help", "-h"):
        print(HELP)
        return 0

    cmd = argv[0]
    if cmd == "listen":
        return cmd_listen(assume_yes="--yes" in argv)
    if cmd == "serve":
        return cmd_serve()
    if cmd == "clean":
        return cmd_clean(argv[1:])
    if cmd == "offline":
        return cmd_offline(argv[1:])
    if cmd == "doctor":
        return cmd_doctor()
    if cmd == "config":
        return cmd_config(argv[1:])

    # Default: everything is one utterance. Flags are stripped out.
    speak_aloud = "--quiet" not in argv and "-q" not in argv
    assume_yes = "--yes" in argv
    words = [a for a in argv if a not in ("--quiet", "-q", "--yes", "--speak")]
    utterance = " ".join(words).strip()
    if not utterance:
        print(HELP)
        return 0
    return cmd_oneshot(utterance, speak_aloud=speak_aloud, assume_yes=assume_yes)


if __name__ == "__main__":
    raise SystemExit(main())
