#!/bin/bash
# Creates a long-lived Claude token for Box Inventory and saves it to .claude-token (readable only by you).
# Run it in your own terminal (on the Mac mini, or on any computer where you then copy the folder).
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE="$(command -v claude || ls "$HOME/.local/bin/claude" 2>/dev/null || true)"
[ -z "$CLAUDE" ] && { echo "claude not found. Install Claude Code: curl -fsSL https://claude.ai/install.sh | bash"; exit 1; }

echo "Step 1: Claude will open a browser to sign in, then print a token (starts with sk-ant-oat…)."
echo "        Copy that token."
echo
"$CLAUDE" setup-token
echo
read -r -s -p "Step 2: Paste the token here and press Enter (it won't be shown): " TOKEN
echo
TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
[ -z "$TOKEN" ] && { echo "No token pasted. Nothing saved."; exit 1; }

umask 077
printf '%s\n' "$TOKEN" > "$DIR/.claude-token"
chmod 600 "$DIR/.claude-token"

echo "Checking the token…"
if env -u CLAUDECODE CLAUDE_CODE_OAUTH_TOKEN="$TOKEN" "$CLAUDE" -p "Reply with just: ok" --no-session-persistence >/dev/null 2>&1; then
  echo "Saved and working: $DIR/.claude-token"
else
  echo "Saved to $DIR/.claude-token, but a test call failed. Run this script again to make a new token."
  exit 1
fi
