#!/usr/bin/env bash
# Build the Prowl on-device speech-to-text helper (prowl-listen).
#
# Compiles prowl/helpers/prowl-listen.swift into a native binary and, when
# possible, ad-hoc code-signs it with mic + speech entitlements so macOS shows
# the correct permission prompts. Best-effort: signing failures warn, not fail.

set -euo pipefail

# Resolve repo root relative to this script (scripts/ lives at the top level).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

SRC="prowl/helpers/prowl-listen.swift"
OUT="prowl/helpers/prowl-listen"

if [[ ! -f "${SRC}" ]]; then
    echo "error: source not found: ${SRC}" >&2
    exit 1
fi

echo "==> Compiling ${SRC}"
swiftc -O -o "${OUT}" "${SRC}" \
    -framework Foundation \
    -framework AVFoundation \
    -framework Speech

echo "==> Compiled ${OUT}"

# --- Ad-hoc code-sign with usage-description entitlements (best-effort) -------
ENT="$(mktemp -t prowl-listen-entitlements-XXXXXX).plist"
cleanup() { rm -f "${ENT}"; }
trap cleanup EXIT

cat > "${ENT}" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.device.audio-input</key>
    <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>Prowl listens to your voice to transcribe commands on-device.</string>
    <key>NSSpeechRecognitionUsageDescription</key>
    <string>Prowl transcribes your speech locally to run voice commands.</string>
</dict>
</plist>
PLIST

if command -v codesign >/dev/null 2>&1; then
    echo "==> Ad-hoc code-signing with mic + speech entitlements"
    if codesign --force --sign - --entitlements "${ENT}" "${OUT}" 2>/tmp/prowl-codesign.err; then
        echo "==> Code-signed ${OUT}"
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
echo "Build complete: ${OUT}"
echo ""
echo "Next steps:"
echo "  * The FIRST run will trigger macOS Microphone and Speech Recognition"
echo "    permission prompts. You must grant BOTH for transcription to work."
echo "  * If prompts do not appear or transcription stays empty, open"
echo "    System Settings > Privacy & Security and enable your terminal (or the"
echo "    Prowl app) under BOTH 'Microphone' and 'Speech Recognition'."
echo "  * Test it:  ${OUT} 12 en-US"
