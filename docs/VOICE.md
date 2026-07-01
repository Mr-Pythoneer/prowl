# Voice: TTS and STT

Prowl's voice layer is deliberately thin and local. Nothing here talks to the
network:

- **Text-to-speech (TTS)** uses the macOS `say` command (built in, no install).
- **Speech-to-text (STT)** uses a tiny native Swift helper (`prowl-listen`) built
  on **Speech.framework + AVFoundation** with **on-device** recognition, so your
  audio never leaves the Mac.

Both degrade gracefully: if TTS breaks, the text is still shown; if STT can't
capture anything, Prowl just says it didn't catch that. Neither ever crashes the
agent.

All voice settings live in `~/.prowl/config.json`. Read/edit them with
`prowl config get` / `prowl config set <key> <value>`, or by editing the JSON
directly. Missing keys fall back to defaults, so a fresh machine works with no
config file at all.

---

## Text-to-speech (`say`)

TTS is `prowl/voice/tts.py`, a stdlib-only wrapper around `say`. Two knobs
control it:

| Config key  | Default      | Meaning                                             |
|-------------|--------------|-----------------------------------------------------|
| `tts_voice` | `"Samantha"` | macOS `say` voice name. `""` = system default voice |
| `tts_rate`  | `190`        | Speech rate in words per minute (int)               |

There is also a master switch:

| Config key      | Default | Meaning                                            |
|-----------------|---------|----------------------------------------------------|
| `voice_enabled` | `true`  | When `false`, replies are shown but never spoken    |

### Choosing a voice

List every installed voice with the `say` command itself:

```bash
say -v '?'
```

Each line is `Name    locale    # sample sentence`, for example:

```
Samantha           en_US    # Hello, my name is Samantha...
Daniel             en_GB    # Hello, my name is Daniel...
```

Preview one before committing to it:

```bash
say -v Daniel -r 190 "Prowl is listening."
```

Want higher-quality voices? macOS ships extras on demand under
**System Settings > Accessibility > Spoken Content > System Voice > Manage
Voices…** (e.g. the "Enhanced"/"Premium" variants). Once downloaded, they appear
in `say -v '?'` and can be used by name.

### Setting the voice and rate

```bash
prowl config set tts_voice Daniel
prowl config set tts_rate 175
```

Or edit `~/.prowl/config.json` directly:

```json
{
  "tts_voice": "Daniel",
  "tts_rate": 175,
  "voice_enabled": true
}
```

The exact command Prowl runs is equivalent to:

```bash
say -v <tts_voice> -r <tts_rate> "<the reply text>"
```

If `tts_voice` is `""`, the `-v` flag is omitted and the system default voice is
used. Anything unparseable (e.g. a non-integer rate) silently falls back to the
defaults `Samantha` / `190`.

---

## Speech-to-text (native Swift helper)

STT is a two-part design:

- **`prowl/helpers/prowl-listen.swift`** — a small native binary that captures
  the microphone and runs `SFSpeechRecognizer` with
  `requiresOnDeviceRecognition = true`. It streams partial results, stops on the
  first of {final result, ~1.5 s of silence after speech begins, or the max-
  seconds cap}, and prints **only the final transcript** to stdout. All status
  and errors go to stderr.
- **`prowl/voice/stt.py`** — a stdlib-only Python wrapper that locates the
  compiled binary, runs it as a subprocess, and returns the transcript (or `""`
  on any failure). It never raises.

Because recognition is `requiresOnDeviceRecognition`, transcription is **offline
and private** — no audio is uploaded. (If a locale has no on-device model, the
helper logs a warning and falls back to server recognition for that run; keep
the default `en-US`, which is on-device on Apple Silicon, to stay fully offline.)

Two config keys tune capture:

| Config key        | Default   | Meaning                                          |
|-------------------|-----------|--------------------------------------------------|
| `stt_locale`      | `"en-US"` | Recognizer locale (BCP-47, e.g. `en-GB`, `de-DE`)|
| `stt_max_seconds` | `12`      | Hard cap on a single dictation, in seconds        |

### Building the helper

The binary is **not** shipped precompiled — you build it once, locally, with no
downloads (it uses only Apple system frameworks):

```bash
bash scripts/build_stt.sh
```

This compiles the Swift source to `prowl/helpers/prowl-listen` with:

```bash
swiftc -O -o prowl/helpers/prowl-listen prowl/helpers/prowl-listen.swift \
    -framework Foundation -framework AVFoundation -framework Speech
```

and then **ad-hoc code-signs** the binary with Microphone + Speech Recognition
entitlements so macOS attributes the permission prompts to the helper. Code-
signing is best-effort: if `codesign` fails or is missing, the script warns and
leaves the binary unsigned — it still works, but the OS attributes the prompts to
the parent app (your Terminal) instead.

Requires the Xcode command-line tools (`swiftc`). If missing:

```bash
xcode-select --install
```

Verify the build:

```bash
python3 -m prowl doctor        # reports whether the STT helper is built
prowl/helpers/prowl-listen 12 en-US   # run it directly (see below)
```

### The helper CLI

```
prowl-listen [maxSeconds] [locale]
```

- `maxSeconds` — capture cap; defaults to `12` if omitted or non-positive.
- `locale` — recognizer locale; defaults to `en-US` if omitted or empty.

`stt.py` invokes it as `[prowl-listen, <stt_max_seconds>, <stt_locale>]` and
gives the subprocess a little extra wall-clock slack over `maxSeconds` for
recognizer warm-up and final flush. On success the helper exits `0` and prints
the transcript; on failure it exits non-zero and `stt.py` returns `""`.

Exit codes (for debugging a direct run):

| Code | Meaning                                                        |
|------|----------------------------------------------------------------|
| `0`  | Success (transcript on stdout; may be empty if nothing heard)  |
| `2`  | Speech Recognition permission denied / restricted / undetermined|
| `3`  | Microphone permission denied                                   |
| `4`  | No recognizer available for the requested locale               |
| `5`  | Recognition error                                              |
| `6`  | No audio input / audio engine failed to start                  |

---

## Permissions (first run)

Prowl uses **no** private entitlements or servers — it relies on the standard
macOS privacy prompts. The **first time** the helper runs it triggers two
system prompts, in order:

1. **Speech Recognition** — "Prowl transcribes your speech locally to run voice
   commands."
2. **Microphone** — "Prowl listens to your voice to transcribe commands
   on-device."

You must **Allow both**. If either is denied, `prowl-listen` exits non-zero
(codes `2`/`3` above) and Prowl reports that it didn't catch anything.

### Granting or re-granting later

Open **System Settings > Privacy & Security** and enable the app under **both**:

- **Microphone**
- **Speech Recognition**

The catch: macOS attributes permission to the process that owns the session. If
the helper is code-signed (the default from `build_stt.sh`), grant **Prowl**
itself. If it's unsigned, or you run `prowl listen` from a terminal, the prompt
is attributed to the **parent app** — so grant your **Terminal** (or iTerm, VS
Code, etc.) under both **Microphone** and **Speech Recognition**. If you later
run Prowl from a different terminal or from the menu-bar app, grant that one too.

After changing these toggles, quit and relaunch the app (or terminal) so the new
grants take effect.

---

## Hotkey and `prowl listen`

There are two ways to talk to Prowl.

### One-shot from the terminal

```bash
python3 -m prowl listen      # or: prowl listen
```

This captures **one** voice turn (`listen_once`), prints `Heard: …`, then routes
and acts on it. If nothing is captured it prints "Didn't catch anything." and
exits non-zero.

### Global hotkey (menu-bar app)

Run the always-on app and press the hotkey anywhere:

```bash
python3 -m prowl serve       # or: prowl serve
```

Default hotkey: **⌘⇧Space** (Command + Shift + Space). Pressing it captures one
voice turn — the same flow as the menu bar's "Talk" item.

The hotkey is configurable via the `hotkey` key, using **pynput** syntax:

| Config key | Default                  | Notes                              |
|------------|--------------------------|------------------------------------|
| `hotkey`   | `"<cmd>+<shift>+space"`  | pynput global-hotkey combo string  |

```bash
prowl config set hotkey "<cmd>+<alt>+p"
```

Global hotkeys are handled by `pynput` (installed via `pip install -r
requirements.txt`). On macOS, capturing global key presses requires granting the
parent app (Terminal, or the Prowl app) **Accessibility** and/or **Input
Monitoring** under **System Settings > Privacy & Security**. The hotkey is
optional: if `pynput` is missing or the listener fails to start, the menu-bar app
still launches and its "Talk" menu item works. `⌘⇧Space` is also the default
macOS Spotlight/input-source shortcut on some setups — if it doesn't fire,
rebind either Prowl's `hotkey` or the conflicting system shortcut.

---

## Troubleshooting

**"Didn't catch anything." / empty transcript**
- Confirm the helper is built: `python3 -m prowl doctor`.
- Run it directly to see stderr diagnostics:
  `prowl/helpers/prowl-listen 12 en-US`.
- Check the input device: **System Settings > Sound > Input** (right mic
  selected, input level moving when you speak).
- Speak sooner — capture ends after ~1.5 s of silence *once speech has begun*,
  and hard-stops at `stt_max_seconds`. Raise the cap if you need longer turns:
  `prowl config set stt_max_seconds 20`.

**Permission denied (exit 2 or 3)**
- Grant **both** Microphone and Speech Recognition to the right app (see
  Permissions above). For terminal use that's your **Terminal/iTerm/VS Code**;
  for a code-signed helper it's **Prowl**.
- Toggle the switch off and back on, then relaunch the app/terminal.
- If no prompt ever appeared, the process may have been denied silently before —
  add it manually under Privacy & Security.

**"prowl-listen helper not found" / not executable**
- The binary hasn't been built (or an arch/permissions issue). Rebuild:
  `bash scripts/build_stt.sh`.
- Ensure `swiftc` exists (`xcode-select --install`).
- The helper is arm64-native; run it on Apple Silicon. If you see an arch
  mismatch, delete `prowl/helpers/prowl-listen` and rebuild.

**No recognizer for locale (exit 4)**
- The requested `stt_locale` has no model. Fall back to `en-US`
  (`prowl config set stt_locale en-US`), or install the language's dictation
  model under **System Settings > Keyboard > Dictation**.

**No audio input (exit 6)**
- No usable input device. Plug in / select a microphone under **Sound > Input**
  and retry.

**TTS is silent**
- Verify `voice_enabled` is `true`: `prowl config get voice_enabled`.
- Verify the voice name exists: `say -v '?'` — a bad `tts_voice` silently falls
  back to `Samantha`. Test directly: `say -v Samantha "test"`.
- Check system output volume and the selected output device.

**Hotkey doesn't fire**
- Grant the parent app **Accessibility** / **Input Monitoring** under Privacy &
  Security, then relaunch.
- Ensure `pynput` is installed (`pip install -r requirements.txt`).
- Resolve conflicts with the default `⌘⇧Space` by rebinding `hotkey` or the
  system shortcut. The menu bar's "Talk" item always works as a fallback.
