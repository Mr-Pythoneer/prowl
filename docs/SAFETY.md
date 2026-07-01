# Prowl Safety Model

Prowl can delete files and run shell commands, so safety is designed in, not
bolted on. This document is the reference for *exactly* what Prowl will and
won't do on its own, where the guardrails are, and how to make it more cautious.

The short version:

- **Cleanup measures first and defaults to dry-run.** Reclaimable junk goes to
  the **Trash** (recoverable). Permanent operations are opt-in and each ask for
  their own extra confirmation.
- **The shell skill has a denylist backstop** for catastrophic commands, and
  always confirms — but it is a seat belt, not a sandbox.
- **Escalation inherits OpenClaw's exec policy.** If OpenClaw is "yolo",
  escalated tasks run shell without prompting.
- **Everything is logged** to `~/.prowl/logs/prowl.log`.
- **The router fails safe toward chat**, never toward a destructive skill.
- **Local model and skills are offline.** Only escalation and web/search leave
  the machine; speech-to-text is on-device.

---

## 1. Cleanup skill — measure first, recoverable by default

The `cleanup` skill (`prowl/skills/cleanup.py`) reclaims disk space. Its whole
design goal is that a mistake can be undone.

### How a run proceeds

1. **Measure.** Every selected category is sized with `du` *before* anything
   moves. Categories that measure as 0 bytes are dropped.
2. **Report.** The user sees a per-category breakdown and a total.
3. **Dry-run by default.** `prowl clean` (no `--apply`) only reports. The CLI
   and `ctx.dry_run` path never touch the disk. Nothing is removed until you
   explicitly ask to apply.
4. **Confirm once** for the whole recoverable batch (when `confirm_destructive`
   is on, which is the default).
5. **Apply.** Recoverable categories are moved to the **Trash** via the native
   macOS Trash API (`NSFileManager.trashItemAtURL:`), with a Finder
   `delete`-to-Trash fallback. Prowl never `rm -rf`s a user cache — it trashes
   it, so it can be restored from the Trash.
6. **Permanent categories each ask again.** Any category marked *permanent* gets
   its **own extra confirmation** immediately before it runs, labelled
   "permanent", so it can be skipped individually even after you approved the
   batch.

The skill deliberately manages its own confirmation (so it can show sizes
first) and is therefore **not** marked `destructive` in its spec — otherwise the
executor would confirm blindly before you had seen what would be removed.

### Categories and their recoverability

| Category key         | What it clears                          | Method                     | Recoverable?              |
|----------------------|-----------------------------------------|----------------------------|---------------------------|
| `user_caches`        | `~/Library/Caches`                      | move to Trash              | **Recoverable** (Trash)   |
| `user_logs`          | `~/Library/Logs`                        | move to Trash              | **Recoverable** (Trash)   |
| `xcode_deriveddata`  | `~/Library/Developer/Xcode/DerivedData` | move to Trash              | **Recoverable** (Trash)   |
| `ios_device_support` | Xcode `iOS DeviceSupport`               | move to Trash              | **Recoverable** (Trash)   |
| `simulator_caches`   | `CoreSimulator/Caches`                  | move to Trash              | **Recoverable** (Trash)   |
| `trash`              | Empty the Trash                         | Finder `empty trash`       | **PERMANENT** (extra confirm) |
| `npm_cache`          | npm cache (`~/.npm/_cacache`)           | `npm cache clean --force`  | **PERMANENT** (extra confirm) |
| `pip_cache`          | pip cache                               | `pip cache purge`          | **PERMANENT** (extra confirm) |
| `homebrew_cache`     | Homebrew downloads                      | `brew cleanup -s`          | **PERMANENT** (extra confirm) |
| `ds_store`           | `.DS_Store` files                       | delete (regenerate on use) | **PERMANENT** (extra confirm) |
| `pycache`            | `__pycache__` dirs                      | delete (regenerate on use) | **PERMANENT** (extra confirm) |

Notes:

- The five Trash-based categories are fully recoverable: open the Trash and put
  the items back. They are only purged for good when you also empty the Trash.
- The four cache-purge categories (`trash`, `npm_cache`, `pip_cache`,
  `homebrew_cache`) delegate to the tool's own purge command; those are
  **permanent** — the bytes are gone, though the caches simply refill as you
  work.
- `ds_store` and `.pycache` are permanent deletes but harmless: macOS and
  Python regenerate them on demand.
- Which categories run by default is set by `cleanup_categories` in config;
  `all` selects the configured default set. The permanent categories above are
  never removed without their own explicit confirm.

---

## 2. `run_shell` skill — denylist backstop, confirmation, no sandbox

The `run_shell` skill (`prowl/skills/shell.py`) executes a free-form shell
command the router/LLM produced from a spoken request. It is powerful and
inherently risky, so three guards apply.

### The guards

1. **Confirmation.** `run_shell` is marked `destructive=True`, so the executor
   confirms before it runs (when `confirm_destructive` is on).
2. **Denylist backstop.** The command is refused outright if it matches a small
   set of clearly catastrophic patterns (case-insensitive):
   - `sudo` — no privilege escalation.
   - `shutdown` / `reboot` / `halt` — no powering down the machine.
   - `mkfs` — no formatting a filesystem.
   - `dd` — can overwrite disks.
   - fork bombs (`:(){` and friends).
   - `rm -rf` (in any flag order) targeting **root, home, `$HOME`, or a bare
     glob** — a specific subpath like `~/Downloads/junk` is allowed through, but
     wiping `/`, `~`, or `*` is refused.
   - redirects into raw disk devices (`> /dev/disk*`, `sd*`, `rdisk*`, `hd*`).
   - recursive `chmod`/`chown` on `/`.
3. **Toggle + limits.** Obeys `shell_skill_enabled` in config (can be turned off
   entirely), runs with a 60-second timeout, captures output (truncated to
   1500 chars), and runs in the home directory.

### This is a backstop, NOT a sandbox

The denylist is a thin seat belt over a **real shell running as you**. There is
**no containment, no capability dropping, and no filesystem isolation**. A
determined or cleverly-worded command can still do damage that the denylist
doesn't anticipate — the patterns are deliberately broad but they are pattern
matches, not a security boundary. Treat `run_shell` as "confirm before I run
this exact string as myself", nothing stronger.

For anything resembling real agentic work (multi-step edits, long-running jobs,
tasks that need judgement), **prefer escalation to OpenClaw**: it runs a full
agent that reasons about each step, rather than firing one opaque string.

---

## 3. Escalation inherits OpenClaw's exec policy

When the router decides a task is too open-ended for a built-in skill, Prowl
hands the whole task to OpenClaw (`openclaw agent`, Claude Opus 4.8, with shell
access) — see `prowl/brain/escalate.py`. **No API keys live in Prowl.**

Crucially, **Prowl does not add a second confirmation layer around escalated
shell**. Once a task is escalated, what the agent is allowed to do is governed
entirely by **OpenClaw's own exec policy**, not by Prowl:

- If OpenClaw is set to **"yolo"**, escalated tasks run shell **without
  prompting**. Prowl will have said "this one needs the smart agent" and then
  the agent proceeds under its own policy.
- If OpenClaw is set to a more cautious preset, the agent asks before running
  commands, exactly as it would outside Prowl.

### How to tighten it

Set OpenClaw's exec policy to a cautious preset so escalated tasks ask before
touching the system:

```bash
openclaw exec-policy preset cautious
```

Check the current policy the same way you would for any OpenClaw session; Prowl
inherits whatever you set. If you want escalation off entirely, set the
escalation backend to `off` (`prowl config set escalation_backend off`) or turn
on offline mode (see §6) — escalation is disabled whenever offline mode is on.

---

## 4. Logging and audit

Every action — especially destructive ones — is logged so it is auditable after
the fact. Logs are written to:

```
~/.prowl/logs/prowl.log
```

The log is rotated (`RotatingFileHandler`, ~2 MB per file, 5 backups) so it
won't grow without bound. What gets recorded includes:

- the utterance heard,
- the router's decision (action + skill),
- each skill invocation with its args and dry-run flag,
- each cleanup category applied and the bytes it reclaimed,
- refused shell commands (the command and the reason it was denied),
- skill failures and exceptions.

`ctx.note(...)` writes to this log without speaking. If something unexpected
happened, this file is the first place to look.

---

## 5. Router fails safe toward chat

The router (`prowl/brain/router.py`) uses the local model **only to classify** a
request into `chat`, `skill`, or `escalate`. It never executes anything itself.

Its ambiguity behavior is deliberately conservative:

- If the model's output can't be parsed, or the action is unrecognized, the
  router falls back to **`chat`** — the harmless "just answer" path — **never**
  to a skill and never to a destructive action.
- If the model names a skill that doesn't exist or is disabled (a
  hallucination), the router does **not** guess a different skill. It escalates
  (if a backend is available) or falls back to chat — it never substitutes a
  destructive skill on ambiguity.
- If the local model is down entirely, it falls back to escalation (if enabled)
  or to a plain "brain is offline" chat reply.

In short: **on any uncertainty, the router drifts toward doing nothing
harmful**, not toward deleting files or running shell.

---

## 6. Privacy — what stays local, what leaves

Prowl is local-first by design.

**Stays on your Mac (offline):**

- **The local model.** Routing and chat run on `llama3.2:3b` via Ollama on the
  machine. No request text is sent anywhere to classify or answer it.
- **Built-in skills.** Opening apps, volume/brightness, file lookup, and disk
  cleanup are all local subprocess calls. Cleanup never uploads anything.
- **Speech-to-text.** STT uses the native on-device Swift speech helper. Your
  audio is transcribed locally, not streamed to a cloud service.

**Leaves the machine (only when you invoke it):**

- **Escalated tasks.** When a task is handed to OpenClaw/Claude, the task text
  goes to that agent's backend so it can reason and act. This is the main way
  data leaves the machine, and it only happens on the `escalate` path.
- **Web and search skills.** `open_url`, `web_search`, and `open_site` hand a
  URL to your default browser via `open`. Prowl itself doesn't fetch anything,
  but of course the site you open sees the request from your browser.

**Offline mode** (`prowl offline on`, or the config key `offline`) forces the
local-only posture: escalation is disabled while offline mode is on, so nothing
is handed to a cloud agent. The local model, skills, and STT keep working.

---

## How to make Prowl more cautious — checklist

- [ ] **Keep confirmations on.** Ensure `confirm_destructive` is `true` (the
      default). This gates the shell skill and each permanent cleanup category.
- [ ] **Preview cleanup first.** Run `prowl clean` (no `--apply`) to see the
      size report before ever reclaiming; add `--apply` only once you're happy.
- [ ] **Trim cleanup to recoverable categories.** Set `cleanup_categories` to
      the Trash-based ones and leave out permanent purges if you want every
      cleanup to be undoable.
- [ ] **Disable the shell skill** if you never want free-form commands:
      `prowl config set shell_skill_enabled false`.
- [ ] **Tighten OpenClaw** so escalated work asks first:
      `openclaw exec-policy preset cautious`.
- [ ] **Turn escalation off** entirely if you want a fully local assistant:
      `prowl config set escalation_backend off`, or run
      `prowl offline on` (also stops any data leaving the machine).
- [ ] **Watch the audit log** after risky operations:
      `tail -f ~/.prowl/logs/prowl.log`.
