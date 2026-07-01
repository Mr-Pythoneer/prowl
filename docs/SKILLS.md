# Skills

A *skill* is one concrete thing Prowl can do on the desktop: open an app, set
the volume, find a file, clean junk, run a guarded shell command. The router
(the local `llama3.2:3b` model) classifies each utterance and, when it decides
the request maps to a skill, names the skill and produces an `args` dict. The
[`Executor`](../prowl/executor.py) then runs it behind the safety gate.

Every skill declares a `SkillSpec` (name, one-line description, examples, args)
so the router can pick it, and returns a `SkillResult` (`ok`, spoken `speech`,
optional `detail`). See [`prowl/skills/base.py`](../prowl/skills/base.py) for
the contract.

All built-in skills are **stdlib-only**: they shell out to the command-line
tools every Mac already ships (`open`, `osascript`, `screencapture`,
`pbcopy`/`pbpaste`, `pmset`, `mdfind`, `du`, `df`, `sw_vers`) with a timeout and
captured output, tolerate loose/renamed args, and never crash the turn — they
return `SkillResult.fail(...)` instead.

---

## Catalog

Skill names below are the exact `spec.name` values the router routes to.
Destructive skills (marked ⚠) are confirmed by the executor before they run.

### System — `prowl/skills/system.py`

| Skill | What it does | Example utterance |
|---|---|---|
| `open_app` | Open or launch a macOS application by name (`open -a`). | "open Safari" |
| `set_volume` | Set output volume 0–100, or mute/unmute the speakers. | "set volume to 40", "mute" |
| `toggle_dark_mode` | Toggle macOS Dark Mode on/off (or force dark/light via `mode`). | "toggle dark mode", "go light" |
| `screenshot` | Capture the screen to a PNG on the Desktop (region / full / window). | "take a screenshot" |
| `clipboard` | Read the clipboard, or copy text onto it (`pbpaste`/`pbcopy`). | "what's on my clipboard", "copy hello world" |
| `system_status` | Report battery level, free disk space, and the macOS version. | "how's my battery", "give me a status report" |
| `lock_or_sleep` | Lock the screen (default) or put the display to sleep. | "lock my screen", "sleep the display" |

### Files — `prowl/skills/files.py`

| Skill | What it does | Example utterance |
|---|---|---|
| `find_files` | Search files by name or contents via Spotlight (`mdfind`), with an optional `kind` filter and a `find` fallback under `~`. | "find my tax pdf" |
| `open_file` | Open a file or folder in its default app (`open`). | "open ~/Downloads/report.pdf" |
| `reveal_in_finder` | Show a file/folder selected in a Finder window (`open -R`). | "reveal that file in Finder" |
| `recent_downloads` | List the newest files in `~/Downloads`, most recent first. | "what did I just download" |

### Apps — `prowl/skills/apps.py`

| Skill | What it does | Example utterance |
|---|---|---|
| `quit_app` ⚠ | Quit a running application by name (AppleScript). | "quit Safari" |
| `activate_app` | Bring an app to the front, launching it if needed (`open -a`). | "switch to Notes" |
| `list_running_apps` | List the apps currently running with a visible window. | "what apps are open" |
| `media_control` | Play / pause / next / previous in Music or Spotify, whichever is running. | "pause the music", "next track" |

### Web — `prowl/skills/web.py`

| Skill | What it does | Example utterance |
|---|---|---|
| `open_url` | Open a URL in the default browser (scheme optional, e.g. `example.com`). | "open example.com" |
| `web_search` | Run a web search in the browser — Google by default, or `duckduckgo`/`bing`. | "search for the weather in Tokyo" |
| `open_site` | Open a well-known site by shorthand (`github`, `gmail`, `youtube`, `maps`, …) or a bare domain. | "open github" |

### Cleanup — `prowl/skills/cleanup.py`

| Skill | What it does | Example utterance |
|---|---|---|
| `cleanup` | Reclaim disk space by clearing caches, logs, dev junk (Xcode DerivedData, iOS DeviceSupport, CoreSimulator caches), and the Trash. | "clean up my mac", "free up disk space" |

`cleanup` **measures first**, shows a per-category breakdown and total, then asks
once before touching anything. Recoverable categories (caches, logs) move to the
**Trash**, never `rm -rf`. Inherently permanent categories (empty Trash,
`.DS_Store` / `__pycache__` removal, `brew`/`npm`/`pip` cache purges) each ask
for their own extra confirmation. It manages its own confirmation and is
therefore *not* marked `destructive`, so the executor does not confirm blindly
before sizes are shown. Args: `category` (a junk category or `all`, the default)
and `apply` (`true` to reclaim; omitted just reports sizes).

### Shell — `prowl/skills/shell.py`

| Skill | What it does | Example utterance |
|---|---|---|
| `run_shell` ⚠ | Run a guarded free-form shell command in the home directory. | "show me disk usage with df -h" |

`run_shell` runs an arbitrary command (`shell=True`, `cwd=~`, 60s timeout,
output truncated). It refuses commands that hit a small **denylist** of
clearly catastrophic patterns (`sudo`, `rm -rf` of `/`/`~`/glob, `mkfs`, `dd`,
fork bombs, writes to raw disk devices, `shutdown`/`reboot`, recursive `chmod /`),
and obeys `shell_skill_enabled` in config so it can be turned off entirely. The
denylist is a backstop, not a sandbox — for real multi-step work, escalation to
OpenClaw is preferred.

---

## Write your own skill

Adding a skill is three steps. No registry edits by hand, no framework — just a
class with a `spec` and a `run`.

### 1. Create `prowl/skills/<name>.py` with `@register` classes

```python
# prowl/skills/mymodule.py
from .base import Skill, SkillResult, SkillSpec, register


@register
class CoinFlip(Skill):
    spec = SkillSpec(
        name="coin_flip",                       # unique id the router routes to
        description="flip a coin and report the result",
        examples=["flip a coin", "heads or tails"],
        args={},                                # arg name -> human description
        # destructive=True,                     # set to have the executor confirm first
    )

    def run(self, args, ctx) -> SkillResult:
        import secrets
        return SkillResult.say(secrets.choice(["Heads.", "Tails."]))
```

The `@register` decorator instantiates the class and adds it to the global
`REGISTRY` keyed by `spec.name`. A module may define several `@register` classes.

Contract reminders:

- Return a `SkillResult`: `SkillResult.say(speech, detail="", **data)` for
  success (`ok=True`) or `SkillResult.fail(speech, detail="")` for failure.
  Keep `speech` short (it is spoken); put anything longer in `detail`.
- **Never raise.** The executor isolates exceptions, but a clean
  `SkillResult.fail(...)` gives the user a spoken reason.
- Honour `ctx.dry_run`: describe what you *would* do instead of doing it.
- Mark `destructive=True` if the action changes/removes state — the executor
  then confirms via `ctx.confirm(...)` before calling `run` (unless dry-run or
  `confirm_destructive` is off). A skill that must show information *before*
  confirming (like `cleanup`) should stay non-destructive and call
  `ctx.confirm` itself.
- Reach config through `ctx.config.<key>` or `ctx.config.get(key, default)`;
  log-only notes via `ctx.note(msg)`; speak/show a line via `ctx.speak(line)`.
- Keep it stdlib-first. Any shell-out should use `subprocess` with a `timeout`,
  `capture_output=True`, and handle `FileNotFoundError` so a missing tool is a
  clean failure. Do not add third-party imports at module top level — the core
  must stay importable without GUI/voice extras.

### 2. Register the module in `prowl/skills/__init__.py`

Add the module's base name (no `.py`) to `_SKILL_MODULES`:

```python
_SKILL_MODULES = [
    "system",
    "files",
    "apps",
    "web",
    "cleanup",
    "shell",
    "mymodule",   # <- your new module
]
```

`load_all()` imports each listed module on startup, which fires its `@register`
decorators and populates `REGISTRY`. A module that fails to import is logged and
skipped — one broken skill will not sink the app. Order is cosmetic; it only
affects listing order in the router prompt.

### 3. Understand how args flow from the router LLM

`specs_text()` renders every enabled skill (name, description, args) into the
compact catalog shown to the router model. When the router decides a request is
a skill, it returns the `spec.name` plus an `args` dict, which the executor
passes straight into your `run(args, ctx)`.

Those args are produced by a small local model, so treat them as **loose**:

- Keys may be **missing or renamed** — scan a few likely keys and coerce, e.g.
  `str(args.get("app") or args.get("name") or "").strip()`.
- Values arrive as **strings** — `"true"`, `"40"`, `"mute"`. Parse tolerantly
  (the built-ins use small helpers like `_first_str`, `_int_in_range`,
  `_truthy`; see `system.py` / `files.py` for the pattern) and clamp/validate.
- On anything unusable, return `SkillResult.fail("What should I …?")` rather
  than raising — the router fails safe toward chat, never toward a destructive
  skill.

The clearer your `spec.description`, `examples`, and `args` descriptions, the
better the router routes to your skill and fills its args. That spec text is the
only thing the model sees.
