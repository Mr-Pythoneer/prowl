# Prowl Architecture

Prowl is a fast, local-first voice desktop agent for Apple Silicon macOS. It
listens (hotkey or voice), understands with a small local model, and then does
exactly one of three things: answer instantly, run a built-in skill, or hand the
whole task to a full agent. This document explains how the pieces fit together,
why the design is shaped the way it is, and the contracts each module honours.

- Target: Python 3.11+, arm64 macOS (M-series).
- Core dependency policy: **standard library only** in the core; third-party
  libraries (`rumps`, `pynput`, `pyobjc`) live in the menu-bar front-end and are
  never imported by the core path.
- No Electron, no bundled runtime. TTS is the built-in `say` command; STT is a
  tiny native Swift helper compiled on-device.


## 1. Bird's-eye view

```
                       ┌───────────────────────────────────────────────┐
   voice  ───────────► │        FRONT-ENDS (share one Orchestrator)     │
   hotkey ───────────► │  CLI (__main__)  ·  menu bar (ui)  ·  voice     │
   typed text ───────► │  STT: native Swift helper   TTS: macOS `say`    │
                       └───────────────────────────┬───────────────────┘
                                                   │  utterance : str
                                                   │  Context   (speak / confirm)
                                        ┌──────────▼──────────┐
                                        │     Orchestrator     │  prowl/executor.py
                                        │  handle(utterance,   │  single entry point
                                        │          ctx)        │
                                        └──────────┬──────────┘
                                                   │
                                        ┌──────────▼──────────┐
                                        │       Router         │  brain/router.py
                                        │  llama3.2:3b, JSON   │  CLASSIFY ONLY
                                        │  chat / skill /      │  never executes,
                                        │      escalate        │  fails safe → chat
                                        └───┬─────────┬────────┘
                        action="chat"       │         │ action="skill"
                        (or reply)          │         │
                  ┌─────────────────────────┘         │        action="escalate"
                  │                                    │                 │
        ┌─────────▼─────────┐              ┌───────────▼──────────┐  ┌───▼────────────┐
        │    LocalBrain     │              │       Executor       │  │   Escalator    │
        │   brain/local.py  │              │   run_skill(): the   │  │ brain/escalate │
        │  Ollama /api/chat │              │      SAFETY GATE     │  │  subprocess:   │
        │  (stdlib urllib)  │              │  confirm · dry-run · │  │ openclaw agent │
        └─────────┬─────────┘              │  log · crash-isolate │  │  -m … --json   │
                  │                        └───────────┬──────────┘  └───┬────────────┘
                  │                                    │                 │  Claude Opus 4.8
                  │                          ┌─────────▼─────────┐       │  (shell access)
                  │                          │   Skill.run(...)  │       │
                  │                          │   skills/*.py     │       │
                  │                          └─────────┬─────────┘       │
                  │                                    │                 │
                  └──────────────► SkillResult ◄───────┴─────────────────┘
                                        │
                                        ▼
                                  ctx.speak(result.speech)   → shows + says one line
```

Every arrow that leaves the box carries a plain `SkillResult`, and every reply to
the user goes through `ctx.speak`. Nothing below the Orchestrator knows or cares
whether the request came from the CLI, the menu bar, or a spoken turn.


## 2. The two-tier brain

Prowl deliberately runs on **two** brains and picks between them per request.

### Tier 1 — the fast local router (`llama3.2:3b` via Ollama)

`prowl/brain/local.py` (`LocalBrain`) talks to a local Ollama server over HTTP
using only `urllib` from the standard library. It does two jobs:

1. **Instant conversational replies** (`reply()`), one or two spoken sentences.
2. **Structured intent classification** (`chat()` in JSON mode, temperature 0.0),
   which the Router uses to decide what to do.

Because the model is small and local, a classification round-trip is fast enough
to feel like Siri, costs nothing, and works with no network. This is the
"reflex" tier: it must be quick, and it must never be the thing that touches the
filesystem.

### Tier 2 — the capable escalation agent (OpenClaw → Claude, or Claude Code)

`prowl/brain/escalate.py` (`Escalator`) shells out to a full agent for anything
open-ended, multi-step, or genuinely agentic (writing/editing files, organizing
many files, research, coding). Two backends are supported, selected by
`config.escalation_backend`:

- `"openclaw"` (default) — `openclaw agent -m <task> --json` runs one turn of the
  already-configured OpenClaw agent (Claude Opus 4.8, with shell access).
- `"claude"` — `claude -p <task>` runs Claude Code non-interactively.
- `"off"` — never escalate; local model only.

No API keys live in Prowl; the escalation binaries own their own auth.

### Why split it this way — speed vs capability

| Dimension        | Tier 1 (local `llama3.2:3b`) | Tier 2 (OpenClaw / Claude) |
|------------------|------------------------------|----------------------------|
| Latency          | Low (feels instant)          | Seconds to minutes         |
| Cost             | Free / offline               | Metered / online           |
| Capability       | Classification + small talk  | Full agent, shell, files   |
| When it runs     | Every request (as router)    | Only when a task needs it  |

The local model is on the hot path for **every** utterance because routing must
be cheap. The heavyweight agent is invoked **only** when the local model judges
the task too big for a built-in skill — so the common case stays fast and free,
and the rare hard case still gets real capability.

### Offline mode and the single "may we go online?" switch

`Config.escalation_enabled()` is the single source of truth for whether Prowl may
make an online/metered call. It returns `True` only if `escalation_backend` is
set to something other than `off`/empty **and** `offline` is `False`. When
`offline` is on, Prowl never escalates and runs entirely on the local model plus
built-in skills. The Router, the Escalator (`enabled` property), and the
Orchestrator all defer to this so behaviour is consistent everywhere.


## 3. Request lifecycle

A single utterance flows through the system like this:

1. **Capture.** A front-end obtains an `utterance: str` (typed, or transcribed by
   the Swift STT helper) and builds a `Context` with the right `speak`/`confirm`
   callbacks. It calls `Orchestrator.handle(utterance, ctx)`.

2. **Guard + log.** `handle()` strips the utterance; an empty one short-circuits
   to `SkillResult.fail("I didn't catch that.")`. Otherwise it logs
   `heard: <utterance>` via `ctx.note`.

3. **Route.** `Router.decide(utterance)` renders the enabled-skills catalog
   (`skills.specs_text()`) into the system prompt and asks the local model, in
   **JSON mode at temperature 0.0**, for a single decision object:

   ```json
   {"action":"skill|escalate|chat","skill":<name|null>,"args":{},"reply":<string|null>}
   ```

   The model **only classifies** — it never runs anything. The raw output is
   parsed leniently (`_parse`): if it isn't valid JSON, the router tries to
   salvage the first `{...}` block, and if that fails too it falls back to a
   `chat` decision. Then `_validate` clamps an unknown `action` to `chat`, and
   if the model named a skill that doesn't exist or is disabled, it does **not**
   guess — it re-routes to `escalate` (or `chat` when no backend is available).

4. **Dispatch** on `decision.action`:

   - **`skill`** → `Executor.run_skill(decision.skill, decision.args, ctx)` runs
     the named skill behind the safety gate (see §4). The Orchestrator then calls
     `ctx.speak(result.speech)` and returns the result.
   - **`escalate`** → `_escalate()`. If no backend is enabled, it degrades to a
     best-effort local chat answer. Otherwise it speaks a short "give me a
     moment" line, runs the agent, and speaks a **trimmed** version of the reply
     (≤ 400 chars spoken; the full text is returned in `SkillResult.detail`).
   - **`chat`** → `_chat()`. If the router already produced a `reply`, that is
     spoken directly; otherwise `LocalBrain.reply(utterance)` generates a short
     spoken answer. If the local model is unreachable, the user hears a friendly
     "my local brain is offline" line and gets a failing `SkillResult`.

5. **Report.** Whatever branch ran, a `SkillResult` comes back and the reply has
   already been delivered through `ctx.speak`. The front-end may additionally
   surface `result.detail` (e.g. the CLI prints it; the menu bar can show it).

### Fail-safe routing

Routing failures never escalate toward danger. If the local model is down,
`decide()` returns `escalate` only when a backend is available, else `chat`. A
malformed model response degrades to `chat`. A hallucinated or disabled skill
becomes `escalate`/`chat`, never a wrong destructive action. The router's default
direction is always the *safe, non-destructive* one.


## 4. The Executor safety gate

`Executor.run_skill` is the only place a skill is invoked, and it wraps every run
in a safety layer:

- **Unknown / disabled skill** → immediate `SkillResult.fail(...)`; nothing runs.
- **Confirmation for destructive skills.** If `skill.spec.destructive` is true,
  we are not in `dry_run`, and `config.confirm_destructive` is set, the executor
  asks `ctx.confirm("This will <description>. Go ahead?")` first. A "no" cancels
  cleanly with `SkillResult.fail("Okay, cancelled — nothing was changed.")`.
- **Dry-run.** When `ctx.dry_run` is true, destructive skills describe what they
  *would* do rather than acting (and confirmation is skipped, since nothing
  changes). Disk cleanup, for example, is a dry-run report unless applied.
- **Crash isolation.** `skill.run(...)` is wrapped in `try/except`: any exception
  is logged and converted to `SkillResult.fail("That didn't work: <exc>")`. A
  single broken skill can never take down the assistant.
- **Auditability.** Every skill invocation logs its name, args, dry-run flag, and
  outcome to the daily rotating log under `~/.prowl/logs`.

This gate is why skills themselves can be simple: they do not implement
confirmation, dry-run branching for safety, or crash handling of their own — the
executor provides those uniformly.


## 5. Module responsibilities

### `prowl/core/config.py`
Dict-backed `Config` with attribute access (`cfg.model`), `.get(key, default)`,
and disk persistence at `~/.prowl/config.json` (never in the repo). Missing keys
fall back to `DEFAULTS`, so a fresh machine works with no config file at all. A
broken config file is treated as empty rather than bricking the assistant. Owns
`escalation_enabled()`, the single "may we go online?" predicate. Also exposes
`PROWL_HOME`, `CONFIG_PATH`, `LOG_DIR`, and `ensure_home()`.

Notable keys: `ollama_url`, `model`, `model_timeout`, `escalation_backend`,
`escalation_thinking`, `escalation_timeout`, `offline`, `voice_enabled`,
`tts_voice`, `tts_rate`, `stt_locale`, `stt_max_seconds`, `hotkey`,
`confirm_destructive`, `shell_skill_enabled`, `trash_instead_of_delete`,
`cleanup_categories`.

### `prowl/core/context.py`
The `Context` dataclass handed to every skill and to the Orchestrator. It carries
the `config`, a `log`, and two callbacks — `speak(str)` (say + show one line) and
`confirm(str) -> bool` (yes/no) — plus a `dry_run` flag and a `note(msg)` helper
that logs without speaking. A skill never talks to the terminal, the speaker, or
the user directly; it goes through the Context. That is what makes skills
front-end-agnostic: the CLI, the menu bar, and a voice turn each supply their own
`speak`/`confirm` implementations, and the same skill code works under all three.

### `prowl/core/logs.py`
Lightweight logging. `get_logger()` returns a singleton `prowl` logger with a
`RotatingFileHandler` writing to `~/.prowl/logs/prowl.log` (2 MB × 5 backups) and
a quiet console handler (warnings and up). Everything Prowl hears, decides, and
does is appended so actions — especially destructive ones — are auditable.

### `prowl/brain/local.py`
`LocalBrain`: the fast local model client. Stdlib-only HTTP (`urllib`) to
Ollama's `/api/chat`. `chat(system, user, json_mode=, temperature=)` powers the
router; `reply(user)` produces a short spoken conversational answer;
`available()` probes `/api/tags` to confirm the model is present. Raises
`LocalModelError` when Ollama can't be reached, so callers can degrade
gracefully. No third-party dependency, keeping the core importable everywhere.

### `prowl/brain/escalate.py`
`Escalator`: the capable tier. `enabled` reflects the configured backend; `run`
dispatches to `_openclaw` or `_claude`. Both invoke a subprocess with
`capture_output`, a timeout (`escalation_timeout` + 30s slack), and defensive
handling of `FileNotFoundError` (binary not on PATH), `TimeoutExpired`, and
non-zero exit — all surfaced as `EscalationError`. The OpenClaw path builds
`openclaw agent -m <task> --json --thinking <level> --timeout <n>`, parses the
JSON envelope, and extracts the human reply from whichever of
`reply/text/message/output/content/result` is present, degrading to raw text if
the shape changes across versions. No API keys live in Prowl.

### `prowl/brain/router.py`
`Router` + `Decision`. Turns one utterance into a `Decision(action, skill, args,
reply, raw)` using the local model in JSON mode. Contains the fail-safe logic:
lenient JSON parsing/salvage, action clamping, and re-routing of
hallucinated/disabled skills to `escalate`/`chat`. It classifies only; it never
executes.

### `prowl/executor.py`
`Executor` (the §4 safety gate) and `Orchestrator` (the §3 lifecycle).
`Orchestrator.handle(utterance, ctx)` is the **single entry point every front-end
calls**. Its constructor loads all skills, builds the `LocalBrain`, `Router`,
`Executor`, and `Escalator`, so a front-end just constructs one Orchestrator and
calls `handle`.

### `prowl/skills/*`
The built-in actions and their contract/registry (see §6). `base.py` defines
`Skill`, `SkillSpec`, `SkillResult`, `REGISTRY`, and the `@register` decorator.
`__init__.py` imports every listed skill module (triggering registration),
exposes `load_all()` and `specs_text()` (the catalog the router shows the LLM),
and swallows a broken module so it can't sink the app. Current modules: `system`
(open apps, volume, brightness, sleep, lock, screenshot, clipboard), `files`
(find/reveal), `apps` (control frontmost/named app via AppleScript), `web` (open
URL, search), `cleanup` (reclaim disk/junk — destructive, dry-run by default),
and `shell` (guarded free-form shell — destructive).

### `prowl/voice/*`
- `tts.py` — text-to-speech via the macOS `say` command. Stdlib only, never
  raises (TTS is a nicety). `speak` (blocking), `speak_async` (non-blocking,
  supersedes in-flight speech), and `stop`. Voice/rate come from config
  (`tts_voice`, `tts_rate`) with safe fallbacks.
- `stt.py` — a thin, stdlib-only wrapper around the native Swift helper
  `prowl/helpers/prowl-listen` (microphone capture + on-device
  `SFSpeechRecognizer`, compiled by `scripts/build_stt.sh`). `helper_path()`,
  `is_available()`, and `listen_once(cfg)` which runs the helper as
  `[prowl-listen, <max_seconds>, <locale>]` and returns the transcript (or `""`
  on any failure). Keeping recognition in a small native binary means no Python
  audio/ML stack to download.

### `prowl/ui/*`
- `hud.py` — a tiny HUD via `osascript`/AppleScript: `ask_text` (one-line
  prompt), `notify` (transient notification), `choose` (list picker). Stdlib
  only, defensive (user cancel / missing binary / timeout all return `None`),
  and it flattens newlines so pasted text can't smuggle extra AppleScript.
- `hotkey.py` — a global hotkey listener via `pynput` (`start_hotkey`,
  `stop_hotkey`). Because only the menu-bar front-end imports it, its top-level
  `pynput` import is fine; the core stays importable without `pynput`. Callbacks
  are guarded so a bad handler never tears down the listener.
- `menubar.py` — the always-on menu-bar app (`prowl serve` → `run_menubar`). It
  wires the hotkey and voice capture to a shared `Orchestrator`, using `hud.py`
  for `speak`/`confirm`. This is a front-end: it owns third-party GUI deps
  (`rumps`/`pyobjc`) and imports the core lazily, so `import prowl` never pulls
  them in.

### `prowl/__main__.py`
The CLI entry point and a reference front-end. It builds a `Context` whose
`speak` prints (and optionally speaks via `voice.tts`) and whose `confirm` reads
from stdin, then calls `Orchestrator.handle`. Subcommands: one-shot utterance
(the default), `listen` (one voice turn), `serve` (menu bar), `clean` (disk
report; dry-run unless `--apply`), `doctor` (environment check), and `config`
(get/set `~/.prowl/config.json`). The one-shot form needs nothing but the
standard library plus Ollama; GUI/voice forms lazy-import their extra pieces.


## 6. The Skill contract and registry

A **skill** is one concrete thing Prowl can do on the desktop. The contract lives
in `prowl/skills/base.py`:

```python
from prowl.skills.base import Skill, SkillResult, SkillSpec, register

@register
class OpenAppSkill(Skill):
    spec = SkillSpec(
        name="open_app",
        description="open a named application",
        examples=["open Safari", "launch Notes"],
        args={"app": "the application name"},
        # destructive=False by default; set True to require confirmation
    )

    def run(self, args: dict, ctx: Context) -> SkillResult:
        app = args.get("app")
        if not app:
            return SkillResult.fail("Which app should I open?")
        ...
        return SkillResult.say(f"Opening {app}.")
```

Key points:

- **`SkillSpec`** is metadata the router shows the LLM: `name` (unique id),
  `description`, `examples`, `args` (name → description), `destructive`, and
  `enabled`. `skills.specs_text()` renders the enabled specs into the router
  prompt so the model can pick a skill and fill its args.
- **`SkillResult`** is what a skill hands back: `ok`, `speech` (one short line to
  speak/show), optional `detail` (longer text, shown not spoken), and optional
  `data`. Use `SkillResult.say(...)` for success and `SkillResult.fail(...)` for
  failure — never raise from `run`.
- **Args are LLM-produced and untrusted.** They arrive as a `dict` of strings,
  possibly with missing or renamed keys and stringy values (`"true"`, `"50"`).
  Skills must be tolerant: read with `.get`, coerce defensively, and return
  `SkillResult.fail(...)` on bad input rather than crashing. (The executor also
  catches exceptions as a backstop, but skills should degrade gracefully.)
- **Shell-outs** use `subprocess` with a timeout and `capture_output`, and handle
  `FileNotFoundError` — matching the rest of the codebase.
- **Registration.** The `@register` decorator instantiates the class and adds it
  to the module-global `REGISTRY: dict[str, Skill]`. `skills/__init__.py` imports
  each module in `_SKILL_MODULES`, which triggers registration. Adding a skill is
  "drop a file in `skills/`, decorate the class, list the module."
- **Self-veto.** `Skill.available(ctx)` lets a skill disable itself (e.g. a
  required tool isn't installed).


## 7. Front-end layering — one Orchestrator, three faces

Prowl has three front-ends, and they all sit on top of the **same** Orchestrator:

- **CLI** (`__main__.py`) — `speak` prints (optionally via `say`), `confirm`
  reads stdin. Great for scripting and `doctor`.
- **Menu bar** (`ui/menubar.py`) — always-on; a global hotkey (`ui/hotkey.py`)
  and/or voice capture (`voice/stt.py`) feed utterances in; `speak`/`confirm` are
  backed by the AppLeScript HUD (`ui/hud.py`) and `voice/tts.py`.
- **Voice turn** (`prowl listen`) — a single spoken utterance transcribed by the
  Swift helper, then routed like any other.

The only thing that differs between them is the `Context` they build — the
`speak` and `confirm` callbacks, and the `dry_run` flag. Because skills and the
router talk exclusively through `Context`, none of the logic below the
Orchestrator changes when you switch front-ends. This is the central seam of the
design: **one brain and one skill set, many faces.**


## 8. Design principles

- **Stdlib-first core.** The core path — config, context, logging, local brain,
  router, executor, escalator, and the skill contract — imports only the standard
  library. `import prowl` and running a one-shot utterance never require
  `rumps`, `pynput`, or `pyobjc`. Third-party GUI/input deps live strictly in the
  front-end modules and are imported lazily.
- **Native where it counts.** Speech synthesis is the built-in `say`; speech
  recognition is a small on-device Swift binary (`SFSpeechRecognizer`); the HUD
  is AppleScript via `osascript`. This gives a real macOS voice experience with
  nothing heavyweight to download.
- **No Electron, arm64-native.** Built for Apple Silicon, with no bundled
  browser/runtime. The footprint is the local model (via Ollama) plus a tiny
  native helper.
- **Offline-capable.** The local model and every built-in skill work with no
  network. `offline` mode (and `escalation_enabled()`) guarantee no
  online/metered call is ever made unless explicitly allowed.
- **Fail-safe routing.** The router only classifies and always defaults toward
  the safe, non-destructive branch (`chat`), never toward a destructive skill.
  Model errors, malformed output, and hallucinated skills all degrade
  predictably.
- **Safety gate before action.** Destructive skills confirm (unless disabled or
  dry-run), disk-touching operations default to dry-run and move to Trash rather
  than delete, every action is logged, and a crashing skill is isolated into a
  clean failure.
- **Extensible by drop-in.** Add a skill by writing one Python file, decorating
  the class with `@register`, and listing the module — no changes to the router
  or executor required.
- **No secrets in Prowl.** Escalation delegates auth to the external `openclaw` /
  `claude` binaries; Prowl holds no API keys.


## 9. Sequence summary

```
front-end        Orchestrator      Router(local)     Executor/skill   Escalator      ctx
   │  utterance ─────►│                                                                │
   │                  │  decide() ──────►│ (JSON, temp 0.0)                            │
   │                  │◄──── Decision ───┤                                             │
   │                  │                                                                │
   │        action == "skill":                                                        │
   │                  │  run_skill() ───────────────►│ (confirm if destructive)       │
   │                  │                               │  skill.run() → SkillResult     │
   │                  │◄──────────────────────────────┤                               │
   │                  │  ctx.speak(result.speech) ────────────────────────────────────►│
   │                  │                                                                │
   │        action == "escalate":                                                     │
   │                  │  run() ─────────────────────────────────────►│ openclaw agent │
   │                  │◄─────────────────── answer ──────────────────┤  -m … --json   │
   │                  │  ctx.speak(trimmed answer) ───────────────────────────────────►│
   │                  │                                                                │
   │        action == "chat":                                                         │
   │                  │  reply() ───────►│ (or decision.reply)                         │
   │                  │◄──── answer ─────┤                                             │
   │                  │  ctx.speak(answer) ───────────────────────────────────────────►│
   │◄─ SkillResult ───┤                                                                │
```
