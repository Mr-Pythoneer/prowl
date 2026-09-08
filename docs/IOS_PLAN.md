# Bob for iOS — build plan

## Context

Siri is bad at understanding, not at permissions. It has every entitlement Apple
can grant and still can't hold context, handle a sentence phrased two ways, or
do two things in one breath. That gap — comprehension, not capability — is the
whole product.

So this is **not** a port of the Mac assistant. Twelve of the Mac's twenty-three
skills are impossible on iOS (no shell, no process control, no system settings,
no screenshots, no floating window). What ports is the part that makes him feel
like an assistant: understanding what you meant, holding things for you, and
doing several steps from one sentence.

**Target device:** iPhone 13 Pro. No Action Button (15 Pro+), no Dynamic Island
(14 Pro+), A15 — so no on-device Apple foundation models. Those three facts
shape most of what follows.

**Prerequisite:** a paid Apple Developer account. On the free Personal Team the
app expires every 7 days and App Groups are unavailable, which breaks the widget
side of the thought cloud. Being bought.

---

## What survives the crossing

| From the Mac | On iOS |
|---|---|
| `prowl-listen.swift` (Speech.framework + AVAudioEngine) | **ports nearly as-is** — both frameworks are iOS-native |
| `say` + Nathan (Enhanced) | `AVSpeechSynthesizer`, same voice |
| DeepSeek routing, `cloud.py` | same API, `URLSession` |
| `prematch.py` + `router.py` logic | **rewrite in Swift** (~600 lines, mostly regex) |
| `timekeeping.py`, `memory.py`, `control.py` | rewrite, straightforward |
| `buddy.py` Core Graphics character | ports to UIKit `draw(_:)` almost directly |
| Ollama fallback | **dropped** — A15 can't; no offline comprehension |
| `claude -p` escalation | Anthropic API directly |
| 12 desktop-control skills | **dropped** |

The Python doesn't come along. The *design* does — and it is the part that took
the work: the three-tier brain, the deterministic pre-router, the safety gates,
and the phrasings the 157 test cases pin down. Those test cases are the
specification for the Swift rewrite.

---

## Architecture

```
Back Tap ×2  ─┐
"Hey Siri…"  ─┼─→  Listener ──→ Router ──→ ┌─ native skill  (EventKit, HomeKit, …)
Lock widget  ─┘   (Speech)      │          ├─ Shortcuts bridge
                                │          ├─ chat / answer
                                │          └─ agent (Anthropic API)
                                │
                         ┌──────┴──────┐
                    prematch        DeepSeek
                    (instant,       (anything
                     offline)        phrased loosely)
                                │
                     BuddyView + Live Activity
```

Same three tiers as the Mac: regex first (free, instant, works offline), cloud
model for the rest, agent for open-ended work. On a phone the first tier matters
more, not less — it is the only thing that works without signal.

---

## Doing things: native first, Shortcuts for the tail

This is the centrepiece and the part that decides whether he beats Siri.

**Tier 1 — native frameworks, no visible bounce:**

| Want | Framework |
|---|---|
| reminders, calendar | EventKit |
| music, podcasts | MPMusicPlayerController / MPRemoteCommandCenter |
| lights, locks, thermostat, scenes | HomeKit |
| timers, alarms | in-app + ActivityKit |
| clipboard | UIPasteboard |
| open an app, deep links | `UIApplication.open` + URL schemes |
| battery, storage, device state | UIDevice / FileManager |
| messages, calls | MessageUI / `tel:` (user confirms send) |

**Tier 2 — the Shortcuts bridge**, for everything Apple only exposes there:
Wi-Fi, Bluetooth, Do Not Disturb, Low Power Mode, brightness, orientation lock,
plus every shortcut you have already built.

Mechanism is `shortcuts://x-callback-url/run-shortcut?name=…&x-success=bob://done`.
Honest cost: **this visibly switches to the Shortcuts app and back.** It is
brief but it is not invisible, which is exactly why Tier 1 exists and why the
common requests belong there.

Discovery problem: iOS gives no API to enumerate a user's shortcuts. So Bob
ships a small **"Bob Toolkit"** — a handful of shortcuts with known names,
installed once — plus a settings screen to register your own by name.

---

## Triggers on a 13 Pro

| | How it feels |
|---|---|
| **Back Tap ×2** (Settings → Accessibility → Touch) | primary. Works through a case, no button to find. ~1s to launch; occasional false fire when setting the phone down |
| **"Hey Siri, ask Bob to…"** via App Intents | true hands-free. Siri transcribes, Bob understands — Siri becomes a doorbell |
| **Lock Screen widget** | one tap from locked |
| **Control Center** | swipe, tap |

There is no custom wake word for third-party apps and there never will be. This
is the honest ceiling: one gesture, then he is listening.

---

## The thought cloud on iOS

No Dynamic Island on a 13 Pro, so it becomes:

* **Live Activity** on the Lock Screen — a timer counting down, updated by
  ActivityKit from the app.
* **Home Screen widget** — remembered notes (needs App Groups, hence the paid
  account).
* **The cloud itself** above his head while the app is open, as on the Mac.

---

## Phases

**Phase 1 — he listens and answers (≈1 week)**
SwiftUI shell, Speech.framework listener ported from `prowl-listen.swift`,
AVSpeechSynthesizer, DeepSeek routing, Swift port of prematch. Skills: time,
date, timers, alarms, memory, clipboard, web. Push-to-talk in-app only.
*Done when:* the Mac's routing test cases pass against the Swift router.

**Phase 2 — he does things (≈1 week)**
EventKit, HomeKit, music, app-opening. Shortcuts bridge plus the Bob Toolkit.
App Intents so Siri can hand off. Back Tap wiring.
*Done when:* "turn off the lights and set an alarm for 6" works in one sentence.

**Phase 3 — he has a face (≈1 week)**
Port `buddy.py` to UIKit — the paperclip, states, tricks, blinking. Live
Activity for timers, widget for notes. Voice picking, settings.
*Done when:* the thought cloud shows a live countdown on the Lock Screen.

**Phase 4 — the agent**
Anthropic API for open-ended requests, with the same confirmation gate the Mac
has. Deliberately last: it is the least differentiated part and the most
expensive to get wrong.

---

## Risks, honestly

* **The Shortcuts bounce may annoy you more than Siri does.** Test it in week
  one with a single shortcut before building the whole bridge.
* **Back Tap reliability varies** with cases and how you hold the phone. If it
  frustrates you, the answer is the Lock Screen widget, not more engineering.
* **No offline comprehension.** No signal means only the regex tier — timers,
  alarms, memory, opening apps. Everything else fails until there is a network.
* **Speech recognition is the same engine Siri uses.** Where Siri mishears you,
  Bob will mishear you too. He recovers better, because he can be corrected and
  because he holds context — but the transcript quality is Apple's, not ours.
* **Battery.** Continuous listening is not possible in the background anyway, so
  this is bounded: he costs nothing while closed.

---

## What would make me stop and rethink

If, after Phase 1, pressing Back Tap doesn't feel meaningfully better than
holding the side button for Siri, the premise is wrong and the remaining two
weeks aren't worth spending. Phase 1 is deliberately shaped to answer that
question cheaply.

---

## Verification

* The Mac's 157 shakedown cases become the Swift router's test suite — same
  utterances, same expected routing. That is the closest thing to a spec.
* Each phase ends with one sentence that must work end to end, spoken aloud, on
  the actual phone — not the simulator, which has no real microphone path.
* Battery: an hour of ordinary use should be indistinguishable from an hour
  without him, since he is only awake while open.
