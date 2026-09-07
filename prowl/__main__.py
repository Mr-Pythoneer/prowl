"""Prowl command-line entry point.

    prowl "what's my ip"          one-shot: route + act, print (and speak) the reply
    prowl listen                  capture one voice turn, then act
    prowl serve                   run the always-on menu-bar app
    prowl clean [--apply]         reclaim disk space (dry-run unless --apply)
    prowl offline [on|off]        local-model-only mode (no online agent calls)
    prowl doctor                  check the environment (brain, escalation, hotkey, voice)
    prowl voices                  list speech voices; `voices try/set <name>`
    prowl config [get|set k v]    read/update ~/.prowl/config.json

The one-shot form is the workhorse and needs nothing but the standard library
plus Ollama; the GUI/voice forms lazy-import their extra pieces.
"""
from __future__ import annotations

import subprocess
import os
import sys

from .core.config import Config, CONFIG_PATH, ensure_home
from .core.context import Context
from .core.logs import get_logger

_SUBCOMMANDS = {"listen", "serve", "clean", "offline", "doctor", "config", "voices", "voice",
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
    from .voice.stt import listen_once_ex

    cfg = Config.load()
    ctx = _make_context(cfg, aloud=cfg.voice_enabled, assume_yes=assume_yes, dry_run=False)
    print("🎙  Listening… (speak now)")
    text, problem = listen_once_ex(cfg)
    if not text:
        # Say why, when we know — a denied microphone otherwise looks exactly
        # like silence, which is impossible to debug from the outside.
        print(problem or "Didn't catch anything — I heard only silence.")
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

    from .brain.brain import Brain
    from .brain.cloud import CloudBrain

    cfg = Config.load()
    print("Prowl doctor\n" + "=" * 40)
    ok = True

    pyver = sys.version.split()[0]
    if sys.version_info >= (3, 11):
        print(f"✅ Python {pyver}")
    else:
        ok = False
        print(f"❌ Python {pyver} — Prowl needs 3.11+. Use the `prowl` launcher or python3.14.")

    offline = bool(cfg.get("offline"))
    print(f"•  Mode: {'OFFLINE (local model only)' if offline else 'online (escalation enabled)'}")

    backend_mode = "local" if offline else str(cfg.get("brain_backend", "auto")).lower()
    print(f"•  Brain: {backend_mode}")

    # Cloud brain (the everyday one when online).
    cb = CloudBrain(cfg)
    if backend_mode == "local":
        print("•  Cloud brain: not used in this mode")
    elif not cb.configured():
        msg = (f"Cloud brain: no API key — set DEEPSEEK_API_KEY, or "
               f"`prowl config set cloud_api_key <key>`")
        if backend_mode == "cloud":
            ok = False
            print(f"❌ {msg}")
        else:
            print(f"•  {msg} (falling back to Ollama)")
    elif cb.available():
        print(f"✅ Cloud brain: {cb.model} at {cb.base}")
    else:
        if backend_mode == "cloud":
            ok = False
        print(f"❌ Cloud brain configured but unreachable ({cb.base})")

    # Local brain (the offline fallback).
    lb = Brain(cfg).local
    if lb.available():
        print(f"✅ Ollama reachable, model '{cfg.model}' present (offline fallback)")
    elif backend_mode == "cloud":
        print(f"•  Ollama not running — no offline fallback available")
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
        if not which("claude"):
            ok = False
            print("❌ claude not on PATH")
        else:
            # On PATH is not the same as logged in — the OAuth session expires.
            import subprocess
            try:
                p = subprocess.run(
                    ["claude", "-p", "--output-format", "text", "say OK"],
                    capture_output=True, text=True, timeout=60,
                )
                blob = (p.stdout + p.stderr).lower()
                if p.returncode == 0:
                    print("✅ Escalation: claude (signed in)")
                elif any(k in blob for k in ("authenticate", "oauth", "logged in", "login")):
                    ok = False
                    print("❌ Escalation: claude is SIGNED OUT — run `claude` "
                          "in a terminal, then /login")
                else:
                    ok = False
                    print(f"❌ Escalation: claude failed ({(p.stderr or p.stdout).strip()[:120]})")
            except subprocess.SubprocessError:
                ok = False
                print("❌ Escalation: claude did not respond in time")

    # The hotkey is the #1 silent failure: a bad string kills the listener and
    # the app just feels dead. Parse it the same way pynput will.
    hk = cfg.get("hotkey", "")
    try:
        from pynput import keyboard as _kb

        from .ui.hotkey import _normalize
        _kb.HotKey.parse(_normalize(hk))
        print(f"✅ Hotkey {hk} parses")
    except ImportError:
        print("•  Hotkey unchecked (pynput not installed — menu bar only)")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"❌ Hotkey {hk!r} is invalid ({exc}) — named keys need <angle brackets>")

    print("✅ `say` (TTS) available" if which("say") else "❌ `say` missing")

    from pathlib import Path
    from .voice.stt import helper_path
    helper = helper_path()
    if not helper.exists():
        print("•  Voice STT helper not built yet — run scripts/build_stt.sh")
    elif not os.access(helper, os.X_OK):
        ok = False
        print(f"❌ Voice STT helper is not executable — chmod +x {helper}")
    else:
        bundled = ".app bundle" if "ProwlListen.app" in str(helper) else "BARE BINARY"
        print(f"✅ Voice STT helper built and executable ({bundled})")
        if bundled != ".app bundle":
            ok = False
            print("❌ helper is not in ProwlListen.app — macOS will kill it on "
                  "first mic use. Rebuild with scripts/build_stt.sh")
        # Speech Recognition / Microphone are TCC-gated; a denial shows up as an
        # instant empty transcript, which is indistinguishable from silence.
        print("•  If voice returns nothing, check System Settings → Privacy & "
              "Security → Microphone and Speech Recognition for Prowl/Terminal")

    print(f"•  Config: {CONFIG_PATH} ({'exists' if CONFIG_PATH.exists() else 'defaults'})")
    print("=" * 40)
    print("All good 🐾" if ok else "Some checks failed — see above.")
    return 0 if ok else 1



def cmd_voices(argv: list[str]) -> int:
    """List installed voices, preview one, or set the voice Prowl speaks with."""
    from .voice import tts

    cfg = Config.load()
    tts.refresh_voices()          # pick up anything installed since last run
    voices = tts.list_voices()
    if not voices:
        print("Couldn't list voices (is `say` available?).")
        return 1

    # `prowl voices set <name>` / `prowl voices try <name>`
    if argv and argv[0] in ("set", "try", "preview"):
        name = " ".join(argv[1:]).strip()
        if not name:
            print(f"usage: prowl voices {argv[0]} <voice name>")
            return 2
        known = {n.lower(): n for n, _ in voices}
        resolved = known.get(name.lower())
        if resolved is None:
            # Allow "Ava" to select "Ava (Premium)".
            matches = [n for n, _ in voices
                       if n.lower() == name.lower() or n.lower().startswith(name.lower() + " (")]
            if not matches:
                print(f"No installed voice matches {name!r}. Run `prowl voices` to see the list.")
                return 1
            resolved = matches[0]
        tts.speak(f"Hi, I'm {resolved.split(' (')[0]}. This is how I sound.",
                  _PreviewCfg(resolved, cfg))
        if argv[0] == "set":
            cfg.set("tts_voice", resolved)
            cfg.save()
            print(f"Voice set to {resolved}.")
        return 0

    tiers = {2: "premium", 1: "enhanced", 0: "compact"}
    current = cfg.get("tts_voice", "auto")
    active = tts.best_voice() if str(current).lower() in ("", "auto", "best") else current
    best_tier = voices[0][1]

    print(f"Voice: {current}" + (f"  (auto -> {active})" if current != active else ""))
    print()
    for name, q in voices:
        mark = "→" if name == active else " "
        print(f" {mark} {name:34} {tiers[q]}")
    print()
    if best_tier == 0:
        print("All of these are macOS's stock 'compact' voices — small, pre-neural,")
        print("and the reason Prowl sounds robotic. Apple's Enhanced and Premium")
        print("voices are free and sound dramatically more human:")
        print()
        print("  System Settings → Accessibility → Spoken Content →")
        print("  System Voice → (i) → Manage Voices… → pick an English voice")
        print("  marked Premium (Ava, Zoe, Evan are good) and download it.")
        print()
        print("Prowl picks the best installed voice automatically, so it will")
        print("start using it as soon as the download finishes.")
    else:
        print("Try one:  prowl voices try \"Ava (Premium)\"")
        print("Keep it:  prowl voices set \"Ava (Premium)\"")
    return 0


class _PreviewCfg:
    """Minimal config shim so a preview can override just the voice."""

    def __init__(self, voice: str, cfg):
        self._voice, self._cfg = voice, cfg

    def get(self, key, default=None):
        if key == "tts_voice":
            return self._voice
        return self._cfg.get(key, default)


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

    # Prowl targets Python 3.11+. The system `python3` on macOS is often 3.9, so
    # warn (don't hard-fail — read-only commands like doctor/config still work)
    # and point at the launcher, which pins a newer interpreter.
    if sys.version_info < (3, 11):
        print(
            f"warning: Prowl targets Python 3.11+, but this is {sys.version.split()[0]}. "
            "Some features may fail — use the `prowl` launcher (scripts/install.sh) "
            "or run with python3.14/python3.13.",
            file=sys.stderr,
        )

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
    if cmd in ("voices", "voice"):
        return cmd_voices(argv[1:])

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
