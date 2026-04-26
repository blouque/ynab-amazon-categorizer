#!/usr/bin/env bash
# install-ynab-wrapper.sh — one-time setup for the `ynab` shortcut.
#
# Installs:
#   - ~/bin/ynab          (the wrapper script)
#   - ~/.zshrc            (adds `ynab` shell function and ensures ~/bin on PATH)
#
# Run this ONCE after you have ynab_cat.py working.

set -euo pipefail

WRAPPER_SRC="${1:-./ynab-wrapper.sh}"
if [ ! -f "$WRAPPER_SRC" ]; then
  echo "ERROR: $WRAPPER_SRC not found. Pass the path to ynab-wrapper.sh as argument." >&2
  exit 1
fi

mkdir -p "$HOME/bin"
cp "$WRAPPER_SRC" "$HOME/bin/ynab"
chmod +x "$HOME/bin/ynab"
echo "  ✓ installed $HOME/bin/ynab"

# Ensure ~/bin is on PATH and a `ynab` shell function exists in .zshrc
ZSHRC="$HOME/.zshrc"
touch "$ZSHRC"

# Idempotent block — bracketed by markers so re-running this doesn't duplicate
MARKER_BEGIN="# ──── ynab-wrapper begin ────"
MARKER_END="# ──── ynab-wrapper end ────"

if grep -qF "$MARKER_BEGIN" "$ZSHRC"; then
  echo "  · ~/.zshrc already has ynab block, leaving it alone"
else
  cat >> "$ZSHRC" <<'EOF'

# ──── ynab-wrapper begin ────
# Adds ~/bin to PATH and defines a `ynab` shell function that calls the
# wrapper script. The function exists so the command stays a builtin-feeling
# one-word invocation regardless of where the script lives.
[ -d "$HOME/bin" ] && case ":$PATH:" in *":$HOME/bin:"*) ;; *) export PATH="$HOME/bin:$PATH" ;; esac
ynab() { command "$HOME/bin/ynab" "$@"; }
# ──── ynab-wrapper end ────
EOF
  echo "  ✓ added ynab function + PATH entry to $ZSHRC"
fi

echo
echo "DONE. Reload your shell:"
echo "  source ~/.zshrc"
echo
echo "Then run: ynab"
