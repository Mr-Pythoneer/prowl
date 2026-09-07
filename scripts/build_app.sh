#!/usr/bin/env bash
# Build Bob.app — a real app bundle around `prowl serve`.
#
# Without this, the menu-bar app runs as "Python": it owns a Dock icon, shows
# up in Cmd-Tab, and Cmd-Q on the wrong window kills your assistant. The bundle
# gives it its own name, its own icon, and LSUIElement so it stays out of the
# Dock and the app switcher entirely — the only way to quit it is its own menu.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${1:-$ROOT/Bob.app}"
NAME="$(basename "$APP" .app)"
PY="${PROWL_PYTHON:-$ROOT/.venv/bin/python}"

if [ ! -x "$PY" ]; then
  echo "error: no interpreter at $PY (run scripts/install.sh first)" >&2
  exit 1
fi

echo "==> Building $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>ai.prowl.bob</string>
    <key>CFBundleName</key>
    <string>$NAME</string>
    <key>CFBundleDisplayName</key>
    <string>$NAME</string>
    <key>CFBundleExecutable</key>
    <string>$NAME</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <!-- Menu-bar only: no Dock icon, not in Cmd-Tab, can't be Cmd-Q'd by accident. -->
    <key>LSUIElement</key>
    <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>$NAME listens for your voice commands.</string>
    <key>NSSpeechRecognitionUsageDescription</key>
    <string>$NAME turns what you say into commands.</string>
</dict>
</plist>
PLIST

# Launcher. Absolute paths throughout: an app bundle inherits almost no
# environment, so PATH and the working directory cannot be relied on.
cat > "$APP/Contents/MacOS/$NAME" <<LAUNCH
#!/bin/bash
export PYTHONPATH="$ROOT"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# Only one copy should hold the menu bar and the microphone.
pkill -f "prowl(\.__main__)? serve" 2>/dev/null
exec "$PY" -m prowl serve
LAUNCH
chmod +x "$APP/Contents/MacOS/$NAME"

# Icon: render the character itself rather than shipping a stock image.
ICONSET="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$ICONSET"
if "$PY" "$ROOT/scripts/render_icon.py" "$ICONSET/icon_512x512.png" 512 2>/dev/null; then
  for sz in 16 32 64 128 256; do
    sips -z $sz $sz "$ICONSET/icon_512x512.png" --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null 2>&1
  done
  cp "$ICONSET/icon_512x512.png" "$ICONSET/icon_256x256@2x.png"
  sips -z 1024 1024 "$ICONSET/icon_512x512.png" --out "$ICONSET/icon_512x512@2x.png" >/dev/null 2>&1
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns" 2>/dev/null \
    && echo "==> Icon built from the character" \
    || echo "==> Icon skipped (iconutil failed)"
else
  echo "==> Icon skipped (renderer unavailable)"
fi

codesign --force --deep -s - "$APP" >/dev/null 2>&1 \
  && echo "==> Ad-hoc signed" || echo "==> Signing skipped"

echo "==> Done: $APP"
echo "    Open it once, then add it in System Settings > General > Login Items"
echo "    to have $NAME start with your Mac."
