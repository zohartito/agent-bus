#!/bin/bash
# Fallback if you are already in this folder. The usual starter is Crew on the Desktop.
# In the repo it finds the repo from here; `crew.py shortcut` pins the path in copies.
REPO="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO"
export PATH="$HOME/.pyenv/shims:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
exec python3 crew.py desk examples/crew-four.toml
