# Improvement plan

Findings from a three-way audit (correctness/concurrency, security, product) plus
direct measurement, ranked by **criticality × ease**. Each item names the file to
change and roughly what it costs.

Effort key: **XS** ≈ minutes · **S** ≈ under an hour · **M** ≈ a few hours · **L** ≈ a day+

---

## Tier 0 — done

**0.1 — `tick_` was running a block of pasted `__init__` code every frame.**
`prowl/ui/buddy.py`. Seven lines of constructor state had been pasted into the
animation tick by a bad edit, so on every frame it forked `pmset` as a
subprocess, cleared `_trick` (so **no trick animation ever played past one
frame**), nulled `_owner` (so the frame-rate throttle was a permanent no-op),
and reset the nap timer (so he could never fall asleep). Fixed by deleting the
block. This is also why typed tricks appeared to do nothing.

---

## Tier 1 — DONE (2026-09-08)

**1.1 — ✅ Escalation runs with no confirmation at all. XS**
`prowl/executor.py` `_escalate`. Destructive *skills* are gated by
`Executor.run_skill`; escalation isn't gated by anything — it goes straight from
a spoken sentence to `claude -p` with shell access. Worse, the router
deliberately sends destructive phrasings there: `_MANAGES_FILES` matches
`delete|remove|move|clean out`, and `_reject` converts those to `escalate`. So
the most dangerous verbs are the ones that skip the gate. Four lines: call
`ctx.confirm` before `self.escalator.run`.

**1.2 — ✅ A brain failure escalates *everything*. XS**
`prowl/brain/router.py` (~line 96). If `Brain.chat` raises `BrainError` — Ollama
down *and* the cloud key rejected — every utterance becomes an agent turn. This
should fail closed: answer "my brain is offline", never escalate.

**1.3 — ✅ The confirmation dialog defaults to Yes. XS**
`prowl/ui/menubar.py` `_confirm`. `default button "Yes"` means Return approves
an arbitrary shell command. One word change to `"No"`, plus `with icon caution`.

**1.4 — ✅ Wake word fires on any sentence starting with "Bob". S**
`prowl/voice/wake.py`. `_PREFIX` is optional, so "Bob was asking about it" wakes
him, arms a 12-second window, and the *next* thing said in the room is executed
as a command. Require the address form ("hey/ok bob"), cut the arm window to
~5s, and require the wake word in the same line for anything that would escalate.

**1.5 — ✅ Trick/control regressions are invisible to the test suite. S**
`scripts/shakedown.py` covers all 20 skills and 54 routing cases but **zero** of
the 12 tricks and 7 control phrases — exactly where the last week's bugs lived,
including 0.1 above. Add both to the corpus.

---

## Tier 2 — DONE (2026-09-08)

**2.1 — ✅ The `run_shell` denylist is trivially bypassable. M**
`prowl/skills/shell.py`. Verified: `rm -rf ~` is blocked, but `rm -rf ~ ;`,
`rm -rf ~ && echo done`, `rm -rf "$HOME"`, `mv ~/Documents /tmp/gone`,
`find ~ -mindepth 1 -delete` and `curl … | bash` all pass. The `\s*$` anchor
means most bypasses are "append one character", and the list only knows `rm`.
A denylist over a full shell cannot be made sound. Replace with an allowlist of
read-only commands, reject shell metacharacters, drop `shell=True` and pass
argv. Anything else routes to escalation (which the module docstring already
says is preferred).

**2.2 — ✅ Two voice turns kill each other and blame the microphone. M**
`prowl/ui/menubar.py` `_talk` has no re-entrancy guard, and `suspend()` is a
bare flag rather than a refcount. Double-tap F5 and turn B's `stop_helpers()`
kills turn A's live capture; turn A then reports *"I couldn't reach the
microphone… grant permission"* — which is false, and is the exact symptom I
chased twice already. Guard `_talk`, make suspend/resume refcounted.

**2.3 — ✅ The whole turn runs on the wake-listener thread. M**
`prowl/voice/wake.py` calls `on_command` synchronously, so the tailing loop is
blocked for the entire turn — up to 150s for an escalation. Consequences: "stop"
cannot be heard while he is working, which defeats the point of control phrases;
and his own speech can be processed as a command once the backlog drains.
Dispatch onto a worker thread, as the hotkey path already does.

**2.4 — ✅ One exception kills wake listening for the session. S**
`prowl/voice/wake.py` `_loop` has no `try` around `self._handle(line)`, and the
callbacks are unguarded. Any exception ends the thread with no log, no
`on_error`, and the menu still showing "listening". Presents as "Hey Bob worked
this morning and doesn't now."

**2.5 — ✅ `cleanup` can skip its own confirmation. S**
`prowl/skills/cleanup.py` is `destructive=False`, so the executor gate never
runs and it self-gates on an `apply` argument — which is *advertised to the
model* in its spec. A model reading "wipe my library caches" has every reason to
send `apply: true`, which skips the prompt. Remove `apply` from the spec so the
router cannot set it; pass it from the CLI under a key the router can't produce.

**2.6 — ✅ `wake.stop()` orphans its thread. S**
It sets `_thread = None` without joining, so a quick stop→start resurrects the
old loop alongside the new one; both then `pkill` each other's recogniser every
10 seconds. Trigger: "stop listening" then "wake up". Join with a timeout and
give each loop a generation token.

---

## Tier 3 — mostly DONE (2026-09-08)

**3.1 — ✅ He doesn't survive a reboot. S**
No LaunchAgent, no login item. `prowl serve` is hand-started and dies on
restart, logout or crash. Every other improvement is worth nothing on the days
he isn't running. Ship `ai.prowl.serve.plist` with `KeepAlive` plus
`prowl autostart on|off`.

**3.2 — ✅ Results dead-end in the GUI. M**
Every skill returns `detail`; the CLI prints it, the menu bar discards it. So
"find my invoice PDF" says *"Found 12 files"* and shows none of them — same for
recent downloads, running apps, the cleanup breakdown, and every Claude answer
(trimmed to 400 chars, remainder dropped). Needs a scrollable results panel and
a "copy that" control phrase.

**3.3 — ✅ He cannot tell the time. S**
No clock, no timer. "What time is it" falls to the model, which has no clock and
**invents** an answer. Wrong-but-confident is the worst failure class here. One
`time_date` skill plus one `timer` skill.

**3.4 — "Stop" cannot cancel work. M**
`_handle_control` maps stop to `tts_stop()` only. An escalation is a blocking
subprocess for up to 150s; say "stop" and nothing happens. Run turns under a
cancellable handle and kill the process group.

**3.5 — ✅ No memory of the previous turn. M**
`"now close it"` routes to `quit_app {'app': 'it'}`. No "do it again", no "the
first one", no undo, and no way to correct a misrecognition except repeating the
whole sentence. Doesn't need LLM history — keep the last `SkillResult` and its
paths, and prematch "again"/"the first one"/"undo" against it.

**3.6 — ✅ Config writes are not atomic. S**
`Config.save` truncates and rewrites with no lock, called from the main thread
*and* the wake-listener thread (on every control phrase). A torn write is caught
by `load` and silently replaced with `{}` — **every setting reverts to defaults
with no warning**. Write to a temp file and `os.replace`; log loudly on a parse
failure instead of discarding.

**3.7 — ✅ The wake transcript file grows forever. S**
`prowl/voice/wake.py` creates it with `delete=False` and never unlinks it, on
every start and every helper restart. With always-listening on, it accumulates
every phrase spoken near the Mac indefinitely and survives quit. A leak and a
privacy artifact.

---

## Tier 4 — worth doing, lower urgency

| | Item | Effort |
|---|---|---|
| 4.1 | **Idle animation costs ~9% of a core plugged in.** Measured: 30fps idle = 9.5%, static idle = 0.67%. Try 12–15fps for a middle ground. | XS |
| 4.2 | **Confirmation is mouse-only** — every destructive action needs a click, in the flow where hands are most likely busy. Accept a spoken yes/no with a timeout. | M |
| 4.3 | **Escalation gets words only** — no clipboard, no frontmost app. "What does this error mean" can't work, and it's one dict away. | S |
| 4.4 | **AppleScript injection via filename** in cleanup's Trash fallback (`cleanup.py:74`). Needs a planted filename, so supply-chain rather than misheard-sentence. `apps.py` already has the `_sanitize` helper. | XS |
| 4.5 | **`find` output split on newlines** then `rm -rf`'d. A directory name containing a newline yields a path that was never matched — and `ds_store`/`pycache` are permanent, not Trash. Use `-print0`. | S |
| 4.6 | **`cfg.save()` freezes all defaults on disk**, so later improvements to defaults are silently ignored. Persist only what differs. | XS |
| 4.7 | **README says data "stays on your Mac"** — untrue since DeepSeek: every utterance missing the fast router goes to their API. | XS |
| 4.8 | **`_on_wake`'s "Yes?" bypasses mute**, so he can hear and route his own acknowledgement; it can also permanently lose the unmute. | S |
| 4.9 | **AppKit read from worker threads** (`is_visible`, `is_mini`) — everything else on `Buddy` hops to the main thread. | S |
| 4.10 | **Timed-out escalation orphans children** (no process group kill); quitting doesn't stop speech mid-sentence. | S |
| 4.11 | **Docs contradict the code** — architecture doc predates the cloud brain, buddy, wake listener and control phrases; roadmap marks shipped features "planned"; installer prints the wrong hotkey. | S |

---

## Suggested order

1. ~~Tier 1 entire~~ — **done**. Every path where a misheard sentence could
   reach an unconfirmed agent is now closed.
2. ~~3.1, 3.3~~ — **done**. Next: **4.1, 4.6, 4.7** — a batch of small wins: he survives reboot, can
   tell the time, stops burning a tenth of a core, and stops lying in the README.
3. ~~2.1 shell allowlist~~ and ~~2.2–2.6 the concurrency cluster~~ — **done**.
4. **3.2, 3.4, 3.5** — the interaction upgrades that make it feel like an
   assistant rather than a voice-triggered command line.

**What not to do:** more skills. Every finding above is in the loop *around* the
skills, and skill #21 gets used once a month.


---

## Still open

* **3.4** — "stop" cannot cancel an in-flight escalation (needs a cancellable
  process handle; the control phrase itself now reaches him instantly, since
  turns no longer block the listener thread).
* **All of Tier 4** — eleven smaller items, headed by the idle animation cost
  and the README's stale privacy claim.
