#!/usr/bin/env bash
# Build the Prowl on-device speech-to-text helper (prowl-listen).
#
# Compiles prowl/helpers/prowl-listen.swift into a native binary, embeds an
# Info.plist carrying the microphone and speech usage descriptions, and ad-hoc
# code-signs it. Best-effort: signing failures warn, not fail.
#
# The .app bundle is NOT cosmetic. macOS TCC reads NSMicrophoneUsageDescription
# and NSSpeechRecognitionUsageDescription from a *bundle's* Info.plist. A bare
# command-line binary does not qualify — not even with the plist linked into a
# __TEXT,__info_plist section (that is sealed by codesign and still ignored by
# TCC, verified on macOS 26). Without a bundle the process is KILLED the instant
# it touches the microphone: SIGABRT, no stderr, no crash report. The only
# visible symptom is voice capture silently returning nothing, which is why this
# looked like "the mic doesn't work" for months.
#
# The tell, if this regresses:
#   log show --last 5m --predicate 'process == "prowl-listen"' --info
#   -> "attempted to access privacy-sensitive data without a usage description"

set -euo pipefail

# Resolve repo root relative to this script (scripts/ lives at the top level).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

SRC="prowl/helpers/prowl-listen.swift"
# The binary lives inside a minimal .app so macOS will read its Info.plist.
APP="prowl/helpers/ProwlListen.app"
OUT="${APP}/Contents/MacOS/prowl-listen"
PLIST_PATH="${APP}/Contents/Info.plist"

if [[ ! -f "${SRC}" ]]; then
    echo "error: source not found: ${SRC}" >&2
    exit 1
fi

# --- Build the .app skeleton -------------------------------------------------
ENT="$(mktemp -t prowl-listen-entitlements-XXXXXX).plist"
cleanup() { rm -f "${ENT}"; }
trap cleanup EXIT

rm -rf "${APP}"
mkdir -p "${APP}/Contents/MacOS"

cat > "${PLIST_PATH}" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>ai.prowl.listen</string>
    <key>CFBundleName</key>
    <string>Prowl</string>
    <key>CFBundleExecutable</key>
    <string>prowl-listen</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSUIElement</key>
    <true/>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>Prowl listens to your voice to transcribe commands on-device.</string>
    <key>NSSpeechRecognitionUsageDescription</key>
    <string>Prowl transcribes your speech locally to run voice commands.</string>
</dict>
</plist>
PLIST

echo "==> Compiling ${SRC} into ${APP}"
swiftc -O -o "${OUT}" "${SRC}" \
    -framework Foundation \
    -framework AVFoundation \
    -framework Speech

echo "==> Compiled ${OUT}"

# --- Ad-hoc code-sign (best-effort) ------------------------------------------
# Usage descriptions deliberately do NOT go here — see the note at the top.
cat > "${ENT}" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.device.audio-input</key>
    <true/>
</dict>
</plist>
PLIST

if command -v codesign >/dev/null 2>&1; then
    echo "==> Ad-hoc code-signing"
    # Sign the whole bundle, so the Info.plist is sealed with the executable.
    if codesign --force --deep --sign - --entitlements "${ENT}" "${APP}" 2>/tmp/prowl-codesign.err; then
        echo "==> Code-signed ${APP}"
    else
        echo "warning: code-signing failed; binary is unsigned. Permission" >&2
        echo "         prompts may be attributed to the parent terminal/app." >&2
        sed 's/^/         codesign: /' /tmp/prowl-codesign.err >&2 || true
    fi
    rm -f /tmp/prowl-codesign.err
else
    echo "warning: codesign not found; skipping entitlements. Permission" >&2
    echo "         prompts may be attributed to the parent terminal/app." >&2
fi

chmod +x "${OUT}"

echo ""
echo "Build complete: ${APP}"
echo "  executable:   ${OUT}"
echo ""
echo "Next steps:"
echo "  * The FIRST run will trigger macOS Microphone and Speech Recognition"
echo "    permission prompts. You must grant BOTH for transcription to work."
echo "  * If prompts do not appear or transcription stays empty, open"
echo "    System Settings > Privacy & Security and enable your terminal (or the"
echo "    Prowl app) under BOTH 'Microphone' and 'Speech Recognition'."
echo "  * Test it:  ${OUT} 12 en-US"
