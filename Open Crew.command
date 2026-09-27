#!/bin/bash
# Fallback if you are already in this folder. The usual starter is Crew on the Desktop.
cd "$(dirname "$0")"
export PATH="$HOME/.pyenv/shims:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
exec python3 crew.py desk examples/crew-four.toml
