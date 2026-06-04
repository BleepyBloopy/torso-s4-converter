#!/usr/bin/env bash
# Double-click this file to launch the Torso S-4 Sample Converter on macOS.
# On first run it will open a Terminal and run setup.sh automatically.
set -e

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "First-time setup — this only runs once."
    echo ""
    ./setup.sh
    echo ""
fi

uv run python -m s4converter.gui
