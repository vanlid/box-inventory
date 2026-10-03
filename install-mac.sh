#!/bin/bash
# Installs Box Inventory as a background service on macOS (starts at login, restarts if it crashes).
# Usage:  ./install-mac.sh            install / reinstall
#         ./install-mac.sh uninstall  stop and remove the service (keeps your data)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="local.box-inventory"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT="${PORT:-8765}"

if [ "$1" = "uninstall" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed the service. Your boxes are still in $DIR/data"
  exit 0
fi

PY="$(command -v python3 || true)"
CLAUDE="$(command -v claude || ls "$HOME/.local/bin/claude" 2>/dev/null || true)"
[ -z "$PY" ] && { echo "python3 not found. Run: xcode-select --install"; exit 1; }
[ -z "$CLAUDE" ] && { echo "claude not found. Install Claude Code: curl -fsSL https://claude.ai/install.sh | bash"; exit 1; }

# Background services don't inherit your shell's variables, so keep an existing env token in .claude-token.
if [ ! -s "$DIR/.claude-token" ] && [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ]; then
  (umask 077; printf '%s\n' "$CLAUDE_CODE_OAUTH_TOKEN" > "$DIR/.claude-token")
  echo "Using CLAUDE_CODE_OAUTH_TOKEN from your shell (saved to .claude-token)."
fi
if [ -s "$DIR/.claude-token" ]; then
  chmod 600 "$DIR/.claude-token"
  export CLAUDE_CODE_OAUTH_TOKEN="$(tr -d '[:space:]' < "$DIR/.claude-token")"
  echo "Checking the saved Claude token…"
  FIX="The token in .claude-token didn't work. Run ./save-token.sh to make a new one."
else
  echo "No .claude-token found; checking this Mac's Claude login…"
  FIX="Claude isn't set up. Run ./save-token.sh (recommended), or run 'claude' once and log in."
fi
if ! "$CLAUDE" -p "Reply with just: ok" --no-session-persistence >/dev/null 2>&1; then
  echo "$FIX"
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$DIR/data"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/server.py</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict>
    <key>PORT</key><string>$PORT</string>
    <key>CLAUDE_BIN</key><string>$CLAUDE</string>
    <key>PATH</key><string>$(dirname "$CLAUDE"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/box-inventory.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/box-inventory.log</string>
</dict></plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 1
echo
echo "Box Inventory is running. On your phone (same Wi-Fi) open:"
echo "   http://$(scutil --get LocalHostName).local:$PORT"
echo "Log: ~/Library/Logs/box-inventory.log"
sleep 2
if [ -f "$DIR/data/setup-code.txt" ]; then
  echo
  echo "Setup code for your first passkey: $(cat "$DIR/data/setup-code.txt")"
fi
echo
echo "Passkey sign-in needs HTTPS. With Tailscale installed: tailscale serve --bg $PORT"
echo "then open the https://<name>.ts.net address it shows."
