# 🐾 Prowl

**A fast, local-AI, voice-first desktop agent for macOS — Siri's convenience with a real agent's brain.**

Prowl listens (hotkey or voice), understands with a **fast local model**
(`llama3.2:3b` via [Ollama](https://ollama.com), instant and offline), and then does one of three things:

1. **Answers instantly** — small talk, quick facts (local model, no network).
2. **Runs a built-in skill** — open apps, set volume/brightness, find files, search the web, **clean junk off your Mac**, and more.
3. **Escalates to a real agent** — anything open-ended, multi-step, or agentic is handed to **[OpenClaw](https://openclaw.ai) → Claude Opus 4.8** (with shell access) via `openclaw agent`.

Think of it as **Siri + OpenClaw**: the snappy front-end of a voice assistant, backed by a local model for speed and a full coding/agent for the hard stuff.

> Built for Apple Silicon (M-series), arm64-native, no Electron. The whole
> front-end uses `pyobjc` (already on most Macs) + a tiny native Swift speech
> helper — so there's no heavyweight runtime to download.

---

## Why it's different from Siri

| | Siri | **Prowl** |
|---|---|---|
| Brains | Cloud, closed | **Local `llama3.2:3b`** for speed + **Claude (OpenClaw)** for hard tasks |
| Can it run shell / edit files / organize folders? | No | **Yes** (via escalation to OpenClaw) |
| Junk cleanup / disk reclaim | No | **Yes**, safely (dry-run, moves to Trash) |
| Offline | Barely | Local model + skills work **offline** |
| Extensible | No | **Drop a Python file in `skills/`** |
| Your data | Apple's servers | **Stays on your Mac** (except escalated tasks) |

---

## Architecture

```
             ┌──────────────────────────────────────────────┐
  voice ───► │  Front-end (menu bar · hotkey · voice HUD)    │
  hotkey ──► │  STT: native Swift (on-device)  TTS: `say`    │
  text ────► └───────────────────────┬──────────────────────┘
                                      │ utterance
                             ┌────────▼────────┐
                             │     Router      │  llama3.2:3b (Ollama, JSON mode)
                             │  chat / skill / │  ── classify only, never executes
                             │    escalate     │
                             └───┬────┬────┬───┘
                    chat ────────┘    │    └──────── escalate
              (local model)           │ skill                 (hard / open-ended)
                                ┌──────▼───────┐        ┌───────────────┐
                                │   Executor   │        │  openclaw     │
                                │ safety layer │        │  agent        │──► Claude Opus 4.8
                                │  + skills    │        │ (shell access)│    (can DO things)
                                └──────────────┘        └───────────────┘
```

- **Router** (`prowl/brain/router.py`) — two stages: a **deterministic pre-router** (`brain/prematch.py`) catches common commands ("pause the music", "clean up my mac", "set volume to 30") instantly with regex — no model call, model-independent — and only the long tail falls through to the local model, which classifies in JSON mode. The router *only decides*, never runs anything, and fails safe toward "chat", never toward a destructive skill.
- **Executor** (`prowl/executor.py`) — runs the chosen skill behind a safety gate (confirmation, dry-run, logging, crash isolation).
- **Escalator** (`prowl/brain/escalate.py`) — shells out to `openclaw agent` (default) or `claude -p`. No API keys live in Prowl.

See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for the full picture and **[docs/SAFETY.md](docs/SAFETY.md)** for the safety model.

---

## Quick start

```bash
# 0. prerequisites
ollama serve &                 # if not already running
ollama pull llama3.2:3b        # ~2 GB, the fast local brain

git clone https://github.com/Mr-Pythoneer/prowl.git
cd prowl

# 1. one-shot install: finds a Python 3.11+, installs the GUI extras, builds the
#    on-device voice helper, and drops a `prowl` launcher on your PATH (pinned to
#    that interpreter, so it survives PATH changes / reboots).
bash scripts/install.sh

# 2. sanity-check + try it
prowl doctor
prowl "what's the capital of France"      # → instant local answer
prowl "open Safari"                        # → skill (deterministic, no model call)
prowl clean                                # → dry-run disk report
prowl offline on                           # → 100% local, no online calls

# 3. run the always-on menu-bar app (⌘⇧Space to talk) + voice
prowl serve
prowl listen
```

> **Python 3.11+ required.** macOS's built-in `python3` is often 3.9 — the
> installer handles this by pinning a newer interpreter into the `prowl`
> launcher. To run without installing: `PYTHONPATH=$PWD python3.14 -m prowl …`

Default hotkey: **⌘⇧Space** → talk. Everything is configurable in `~/.prowl/config.json` (`prowl config get` / `prowl config set`).

---

## Commands

| Command | What it does |
|---|---|
| `prowl "<request>"` | One-shot: route + act, print/speak the reply |
| `prowl listen` | Capture one voice turn, then act |
| `prowl serve` | Run the always-on menu-bar app |
| `prowl clean [--apply]` | Reclaim disk space (dry-run unless `--apply`) |
| `prowl offline [on\|off]` | Local-model-only mode — no online agent calls |
| `prowl doctor` | Check Ollama, model, OpenClaw, voice, config |
| `prowl config [get\|set k v]` | Read/update configuration |

---

## Offline mode (save your wallet 💸)

Prowl has two brains: the **local** `llama3.2:3b` (free, instant, offline) and an
**online** escalation path (OpenClaw → Claude) for hard, agentic tasks. If you'd
rather never make an online call, flip on **offline mode**:

```bash
prowl offline on      # 100% local model — no escalation, no online calls
prowl offline status  # check
prowl offline off     # re-enable escalation for open-ended tasks
```

There's also a toggle in the menu bar. In offline mode:

- Quick answers, and **every built-in skill** (open apps, volume, files, web, **cleanup**, guarded shell, …) still work — they don't need the online agent.
- Open-ended "go do this multi-step thing" requests are answered honestly by the local model, which will tell you when a task needs online mode instead of pretending to run an agent.

> Note: in this setup OpenClaw runs on your **Claude subscription** (it reuses the
> Claude CLI login), not a metered per-token API key — so escalation isn't billing
> you per token. Offline mode still guarantees *zero* online usage if you want it.

---

## Safety (read this before `--apply`)

Prowl can delete files and run shell commands, so safety is built in, not bolted on:

- **Cleanup is dry-run by default** — it *measures and reports* before touching anything, then asks once.
- **Recoverable by default** — reclaimable caches/logs go to the **Trash** (native Trash API), never `rm -rf`.
- **Permanent operations are opt-in** — emptying the Trash, `.DS_Store` removal, and `brew/npm/pip` cache purges each ask for their own extra confirmation.
- **Everything is logged** to `~/.prowl/logs/prowl.log`.
- **Escalation inherits OpenClaw's exec policy.** If OpenClaw is set to "yolo", escalated tasks run shell without prompting — tighten it with `openclaw exec-policy preset cautious` if you want a leash.

Full details in **[docs/SAFETY.md](docs/SAFETY.md)**.

---

## Extending Prowl

Add a skill in three steps:

```python
# prowl/skills/mymodule.py
from .base import Skill, SkillResult, SkillSpec, register

@register
class Flip(Skill):
    spec = SkillSpec(
        name="coin_flip",
        description="flip a coin",
        examples=["flip a coin", "heads or tails"],
    )
    def run(self, args, ctx) -> SkillResult:
        import secrets
        return SkillResult.say(secrets.choice(["Heads.", "Tails."]))
```

Then add `"mymodule"` to `_SKILL_MODULES` in `prowl/skills/__init__.py`. The router will offer it automatically.

---

## Status

Early but working. Core (router, executor, skills, cleanup, CLI) is functional;
voice + menu bar are the front-end layer. Roadmap in **[docs/ROADMAP.md](docs/ROADMAP.md)**.

## License

MIT — see [LICENSE](LICENSE).
