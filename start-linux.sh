#!/bin/bash
# Starts Signalpipe on this computer and opens it in your browser.
# The first start installs uv (a small tool that sets up the right Python) and Signalpipe's packages.
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

pause_and_exit() { echo; read -r -p "Press Enter to close this window." _; exit "$1"; }

if ! command -v uv >/dev/null 2>&1; then
  echo "First start: installing uv, which sets up Python for Signalpipe..."
  if ! curl -LsSf https://astral.sh/uv/install.sh | sh; then
    echo "Could not install uv. Check your internet connection and try again."
    pause_and_exit 1
  fi
fi

if ! command -v claude >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/claude" ]; then
  echo "To run the agents on your Claude subscription, install Claude Code once:"
  echo "  curl -fsSL https://claude.ai/install.sh | bash"
  echo "then run: claude   (and sign in with your Claude account), and start Signalpipe again."
  echo
fi
echo "Starting Signalpipe (the first start downloads its packages, a few minutes)..."
uv run --quiet --no-project --python 3.13 --with-requirements requirements-local.txt python -m app.local "$@"
status=$?
# 0 is a normal stop, 130 is Ctrl+C: both are how you are meant to quit.
if [ "$status" -ne 0 ] && [ "$status" -ne 130 ]; then
  echo "Signalpipe stopped with an error (status $status)."
  pause_and_exit "$status"
fi
exit 0
