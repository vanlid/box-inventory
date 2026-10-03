#!/bin/bash
# Installs Box Inventory as a systemd user service on Linux (starts at boot, restarts if it crashes).
# Usage:  ./install-linux.sh            install / reinstall
#         ./install-linux.sh uninstall  stop and remove the service (keeps your data)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
NAME="box-inventory"
UNIT="$HOME/.config/systemd/user/$NAME.service"
PORT="${PORT:-8765}"

if ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "systemd user services aren't available here. Use Docker instead: docker compose up -d --build"
  exit 1
fi

if [ "$1" = "uninstall" ]; then
  systemctl --user disable --now "$NAME" 2>/dev/null || true
  rm -f "$UNIT"; systemctl --user daemon-reload
  echo "Removed the service. Your boxes are still in $DIR/data"
  exit 0
fi

PY="$(command -v python3 || true)"
CLAUDE="$(command -v claude || ls "$HOME/.local/bin/claude" 2>/dev/null || true)"
[ -z "$PY" ] && { echo "python3 not found. Install it with your package manager (e.g. sudo apt install python3)."; exit 1; }
[ -z "$CLAUDE" ] && { echo "claude not found. Install Claude Code: curl -fsSL https://claude.ai/install.sh | bash"; exit 1; }

# Services don't inherit your shell's variables, so keep an existing env token in .claude-token.
if [ ! -s "$DIR/.claude-token" ] && [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ]; then
  (umask 077; printf '%s\n' "$CLAUDE_CODE_OAUTH_TOKEN" > "$DIR/.claude-token")
  echo "Using CLAUDE_CODE_OAUTH_TOKEN from your shell (saved to .claude-token)."
fi
if [ -s "$DIR/.claude-token" ]; then
  chmod 600 "$DIR/.claude-token"
  TOKEN="$(tr -d '[:space:]' < "$DIR/.claude-token")"
  echo "Checking the saved Claude token…"
  FIX="The token in .claude-token didn't work. Run ./save-token.sh to make a new one."
else
  TOKEN=""
  echo "No .claude-token found; checking this computer's Claude login…"
  FIX="Claude isn't set up. Set CLAUDE_CODE_OAUTH_TOKEN, run ./save-token.sh, or run 'claude' once and log in."
fi
# Test outside any parent Claude Code session so the result reflects what the service will see.
if ! env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_CODE_SESSION_ID ${TOKEN:+CLAUDE_CODE_OAUTH_TOKEN="$TOKEN"} \
     "$CLAUDE" -p "Reply with just: ok" --no-session-persistence >/dev/null 2>&1; then
  echo "$FIX"
  exit 1
fi

mkdir -p "$(dirname "$UNIT")" "$DIR/data"
cat > "$UNIT" <<EOF
[Unit]
Description=Box Inventory
After=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$PY $DIR/server.py
Environment=PORT=$PORT
Environment=CLAUDE_BIN=$CLAUDE
Environment=PATH=$(dirname "$CLAUDE"):/usr/local/bin:/usr/bin:/bin
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable "$NAME" >/dev/null 2>&1
systemctl --user restart "$NAME"

# Keep the service running when you're logged out and after reboots.
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  loginctl enable-linger "$USER" 2>/dev/null \
    || echo "Note: to keep it running after reboot without logging in, run: sudo loginctl enable-linger $USER"
fi

sleep 1
if ! systemctl --user is-active --quiet "$NAME"; then
  echo "The service didn't start. See: journalctl --user -u $NAME -n 30"
  exit 1
fi
echo
echo "Box Inventory is running. On your phone (same Wi-Fi) open one of:"
echo "   http://$(hostname).local:$PORT"
ip -4 -o addr show scope global 2>/dev/null | awk '$2 !~ /^(docker|br-|veth|virbr|cni|flannel)/ {split($4,a,"/"); print "   http://" a[1] ":'"$PORT"'"}'
echo "Logs: journalctl --user -u $NAME -f"
sleep 2
if [ -f "$DIR/data/setup-code.txt" ]; then
  echo
  echo "Setup code for your first passkey: $(cat "$DIR/data/setup-code.txt")"
fi
echo
echo "Passkey sign-in needs HTTPS. With Tailscale installed: tailscale serve --bg $PORT"
echo "then open the https://<name>.ts.net address it shows."
