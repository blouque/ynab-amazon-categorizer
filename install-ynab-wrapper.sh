#!/usr/bin/env bash
# install-ynab-wrapper.sh — one-time setup for the `ynab` shortcut.
#
# Installs:
#   - ~/bin/ynab (the wrapper script)
#   - ~/.zshrc (adds YNAB_HOME, `ynab` shell function, and ensures ~/bin on PATH)
#
# Run this ONCE from inside the project directory after you have ynab_cat.py working.

set -euo pipefail

WRAPPER_SRC="${1:-./ynab-wrapper.sh}"

if [ ! -f "$WRAPPER_SRC" ]; then
  echo "ERROR: $WRAPPER_SRC not found. Pass the path to ynab-wrapper.sh as argument." >&2
  exit 1
fi

# Detect project root from the location of this script — works regardless of
# where the repo was cloned or what it was named.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$HOME/bin"
cp "$WRAPPER_SRC" "$HOME/bin/ynab"
chmod +x "$HOME/bin/ynab"
echo " ✓ installed $HOME/bin/ynab"

ZSHRC="$HOME/.zshrc"
touch "$ZSHRC"

MARKER_BEGIN="# ──── ynab-wrapper begin ────"
MARKER_END="# ──── ynab-wrapper end ────"

if grep -qF "$MARKER_BEGIN" "$ZSHRC"; then
  echo " · ~/.zshrc already has ynab block, leaving it alone"
  echo " · If you moved the project, remove the block and re-run this script."
else
  # $SCRIPT_DIR expands NOW (install time) — that's intentional.
  # \$HOME and \$PATH are escaped so they expand at shell runtime.
  cat >> "$ZSHRC" <<EOF

$MARKER_BEGIN
# Project location — set at install time. If you move the repo, re-run
# install-ynab-wrapper.sh to update this.
export YNAB_HOME="$SCRIPT_DIR"
[ -d "\$HOME/bin" ] && case ":\$PATH:" in *":\$HOME/bin:"*) ;; *) export PATH="\$HOME/bin:\$PATH" ;; esac
ynab() { command "\$HOME/bin/ynab" "\$@"; }
$MARKER_END
EOF
  echo " ✓ added YNAB_HOME, ynab function, and PATH entry to $ZSHRC"
fi

echo
echo "DONE. Reload your shell:"
echo "  source ~/.zshrc"
echo
echo "Then run: ynab"
