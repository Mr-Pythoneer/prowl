#!/usr/bin/env bash
# Install (or remove) the LaunchAgent that keeps Bob running.
#
# Without this he is a hand-started process: he dies on reboot, on logout, and
# on any crash, and never comes back. An assistant that needs a manual start
# stops being used.
#
#   scripts/install_agent.sh          install and start
#   scripts/install_agent.sh --remove stop and uninstall
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="ai.prowl.serve"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
APP="$ROOT/Bob.app/Contents/MacOS/Bob"

if [ "${1:-}" = "--remove" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "==> Removed $LABEL"
  exit 0
fi

if [ ! -x "$APP" ]; then
  echo "error: $APP not found — run scripts/build_app.sh first" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.prowl/logs"

# KeepAlive restarts him if he crashes; RunAtLoad starts him at login.
# ThrottleInterval stops a crash-loop from spinning.
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$APP</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$HOME/.prowl/logs/agent.out.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.prowl/logs/agent.err.log</string>
    <key>ProcessType</key>
    <string>Interactive</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "==> Installed $LABEL"
echo "    Starts at login, and restarts if he crashes."
echo "    Status:  launchctl print gui/$(id -u)/$LABEL | head -5"
echo "    Remove:  scripts/install_agent.sh --remove"
