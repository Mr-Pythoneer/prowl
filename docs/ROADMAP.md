# Roadmap

Where Prowl is and where it's headed. Each item is tagged:

- **[done]** — shipped and working today.
- **[planned]** — designed/agreed, not yet built.
- **[idea]** — under consideration, not committed.

Prowl's shape stays fixed: a fast local model (`llama3.2:3b` via Ollama) routes
each utterance to *chat*, a *built-in skill*, or *escalation* to OpenClaw →
Claude. The roadmap adds reach and polish without changing that core.

---

## v0.1 — Core (done)

The working foundation. Everything here exists in the repo today.

- **[done]** Router — local model classifies each utterance as chat / skill /
  escalate in JSON mode; it only decides, never executes, and fails safe toward
  chat. (`prowl/brain/router.py`)
- **[done]** Executor + safety gate — runs one skill with confirmation for
  destructive skills, dry-run support, logging, and exception isolation.
  (`prowl/executor.py`)
- **[done]** Orchestrator — single entry point every front-end calls; ties
  router, executor, local brain, and escalator together.
- **[done]** Built-in skills — `system` (open_app, set_volume, toggle_dark_mode,
  screenshot, clipboard, system_status, lock_or_sleep), `files` (find_files,
  open_file, reveal_in_finder, recent_downloads), `apps` (quit_app, activate_app,
  list_running_apps, media_control), `web` (open_url, web_search, open_site).
  See [SKILLS.md](SKILLS.md).
- **[done]** Cleanup skill — measure-first disk reclaim; recoverable categories
  move to Trash, permanent ones opt-in. (`prowl/skills/cleanup.py`)
- **[done]** Guarded shell skill — free-form command with a catastrophic-pattern
  denylist, home-dir cwd, timeout, and an on/off config flag.
- **[done]** Escalation — shells out to `openclaw agent` (or `claude -p`) for
  open-ended, multi-step work; long answers trimmed for speech, full text kept.
- **[done]** CLI — `prowl "<request>"`, `prowl clean [--apply]`, `prowl doctor`,
  `prowl config get|set`. (`python3 -m prowl`)
- **[done]** Extensibility — drop a `@register` skill file in `skills/`, add it
  to `_SKILL_MODULES`; the router offers it automatically.

The front-end layer (menu bar, hotkey, voice HUD, native Swift STT, `say` TTS)
exists as scaffolding and is the focus of the next release.

---

## v0.2 — Voice & front-end hardening (planned)

Make the voice/menu-bar experience reliable enough for daily always-on use.

- **[planned]** Voice hardening — robust start/stop of the native Swift STT
  helper, partial-result handling, `stt_max_seconds` enforcement, graceful
  recovery when the mic or helper is unavailable.
- **[planned]** Wake-word / always-listening mode — an opt-in hands-free path
  ("Hey Prowl") in addition to the ⌘⇧Space hotkey, with a clear listening
  indicator and a hard privacy switch.
- **[planned]** Menu-bar polish — richer status (idle / listening / thinking /
  acting), inline results, a skills/history panel, and quick config toggles.
- **[planned]** More skills — brightness control (the `system` module already
  advertises it; the skill itself is not implemented yet), Do Not Disturb /
  Focus, Wi-Fi/Bluetooth toggles, notifications, window arrangement, and a few
  more file operations.
- **[planned]** Router quality — better few-shot prompting and a small offline
  eval set so routing accuracy (and safe-fail behaviour) can be measured across
  changes.

---

## v0.3 — Memory & smarter behaviour (planned)

Move from one-shot commands toward context-aware interactions.

- **[planned]** Conversation memory / context — carry a short rolling context so
  follow-ups ("do it again", "now the other one", "close that") resolve against
  the previous turn.
- **[planned]** Per-app skills — skills that adapt to the frontmost app (e.g.
  "new tab", "save", "next slide") by targeting whatever is in focus.
- **[planned]** Streaming TTS — speak the reply as it's generated instead of
  waiting for the full answer, for snappier long responses.
- **[planned]** Better escalation summaries — a tighter spoken summary of a long
  OpenClaw result, with the full transcript kept in `detail` and viewable.

---

## Later ideas

Directions worth exploring once the above lands. Not committed.

- **[idea]** Multi-turn tasks — let Prowl run a short plan of several skills
  (with confirmation checkpoints) instead of a single action per utterance.
- **[idea]** Shortcuts integration — invoke macOS Shortcuts as skills, so users
  extend Prowl without writing Python.
- **[idea]** Plugin skills — load skills from outside the package (a user
  `~/.prowl/skills/` directory) with clear trust/enable boundaries.
- **[idea]** Local model auto-selection by task — pick the smallest capable
  local model per request (tiny for routing, larger for reasoning) to balance
  latency and quality, escalating to Claude only when genuinely needed.

---

*Legend: **[done]** shipped · **[planned]** committed, not yet built ·
**[idea]** under consideration. Skill names map to `spec.name` values in
`prowl/skills/`.*
