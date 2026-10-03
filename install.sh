#!/bin/bash
# Box Inventory for Mac and Linux: download (or update) and install, no git needed.
#   curl -fsSL https://github.com/vanlid/box-inventory/releases/latest/download/install.sh | bash
# Run it again to update. Your data/, .env and .claude-token are never touched.
# Options: BOX_DIR=~/box-inventory  BOX_VERSION=latest (or v1.2.0)  BOX_NO_SERVICE=1 (download only)
set -euo pipefail
REPO="vanlid/box-inventory"
DIR="${BOX_DIR:-$HOME/box-inventory}"
VERSION="${BOX_VERSION:-latest}"
if [ -n "${BOX_URL:-}" ]; then URL="$BOX_URL"
elif [ "$VERSION" = latest ]; then URL="https://github.com/$REPO/releases/latest/download/box-inventory.tar.gz"
else URL="https://github.com/$REPO/releases/download/$VERSION/box-inventory.tar.gz"; fi

command -v curl >/dev/null || { echo "curl is needed."; exit 1; }
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "Downloading Box Inventory ($VERSION)…"
if ! curl -fsSL "$URL" -o "$TMP/app.tar.gz"; then
  echo "Download failed: $URL"
  echo "If the repository is private, sign in first and download with: gh release download -R $REPO -p box-inventory.tar.gz"
  exit 1
fi
tar -xzf "$TMP/app.tar.gz" -C "$TMP"
SRC="$TMP/box-inventory"
[ -f "$SRC/server.py" ] || { echo "That download doesn't look like Box Inventory."; exit 1; }

mkdir -p "$DIR"
for f in "$SRC"/* "$SRC"/.[!.]*; do
  [ -e "$f" ] || continue
  case "$(basename "$f")" in data|.env|.claude-token) continue ;; esac  # never overwrite your data or keys
  cp -R "$f" "$DIR/"
done
chmod +x "$DIR"/*.sh
[ -f "$DIR/.env" ] || { cp "$DIR/.env.example" "$DIR/.env"; chmod 600 "$DIR/.env"; }
echo "Box Inventory $(cat "$DIR/VERSION" 2>/dev/null || echo "$VERSION") is in $DIR"
[ -n "${BOX_NO_SERVICE:-}" ] && exit 0

case "$(uname -s)" in
  Darwin) exec "$DIR/install-mac.sh" ;;
  Linux) exec "$DIR/install-linux.sh" ;;
  *) echo "On Windows, use install.ps1 instead."; exit 1 ;;
esac
