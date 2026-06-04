#!/usr/bin/env bash
set -e

echo "=== Torso S-4 Sample Converter — Setup ==="
echo ""

# --- Homebrew ---
if ! command -v brew &>/dev/null; then
    echo "ERROR: Homebrew not found."
    echo "Install it from https://brew.sh then re-run this script."
    exit 1
fi

# --- ffmpeg ---
if ! command -v ffmpeg &>/dev/null; then
    echo "Installing ffmpeg..."
    brew install ffmpeg
else
    echo "ffmpeg: already installed"
fi

# --- uv ---
if ! command -v uv &>/dev/null; then
    echo "Installing uv..."
    brew install uv
else
    echo "uv: already installed"
fi

# --- Virtual environment ---
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    uv venv .venv
else
    echo ".venv: already exists"
fi

# --- Python dependencies ---
# Note: aubio 0.4.9 has a C type mismatch with numpy 2.x on Python 3.14.
# The CFLAGS flag suppresses that specific compiler error — safe to use here.
echo "Installing Python dependencies..."
CFLAGS="-Wno-incompatible-function-pointer-types" \
    uv pip install -r requirements.txt --python .venv/bin/python

echo ""
echo "Setup complete."
echo ""
echo "Run the app:"
echo "  uv run python -m s4converter.gui    # GUI"
echo "  uv run python -m s4converter.cli    # CLI"
