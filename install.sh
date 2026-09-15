#!/bin/sh
# install.sh - one-shot installer for the native (Python) ccr on macOS/Linux
#
#   curl -fsSL https://raw.githubusercontent.com/Cepstral/claude-codex-resume/main/install.sh | sh
#
# Puts `ccr` in ~/.local/bin (created if missing). Needs python3 and fzf.
# Re-running updates in place.
set -e
raw="https://raw.githubusercontent.com/Cepstral/claude-codex-resume/main/ccr.py"
dir="${CCR_BIN_DIR:-$HOME/.local/bin}"

command -v python3 >/dev/null 2>&1 || { echo "ccr: python3 is required (macOS: xcode-select --install, or brew install python)"; exit 1; }
command -v fzf >/dev/null 2>&1 || echo "ccr: note - fzf not found; install it before running ccr (brew install fzf)"

mkdir -p "$dir"
if [ -f "$(dirname "$0")/ccr.py" ] && [ "$(basename "$0")" = "install.sh" ]; then
    cp "$(dirname "$0")/ccr.py" "$dir/ccr"          # from a clone
else
    curl -fsSL "$raw" -o "$dir/ccr"                  # from the one-liner
fi
chmod +x "$dir/ccr"
case ":$PATH:" in
    *":$dir:"*) ;;
    *) echo "ccr: add this to your shell rc, then open a new terminal:  export PATH=\"$dir:\$PATH\"" ;;
esac
echo "installed $dir/ccr - run: ccr"
