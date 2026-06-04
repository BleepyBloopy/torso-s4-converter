@echo off
:: Double-click this file to launch the Torso S-4 Sample Converter on Windows.
:: On first run it will run setup automatically.

cd /d "%~dp0"

:: Check for uv
where uv >nul 2>&1
if errorlevel 1 (
    echo uv not found. Install it from https://docs.astral.sh/uv/getting-started/installation/
    echo Then re-run this file.
    pause
    exit /b 1
)

:: Check for ffmpeg
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo ffmpeg not found. Install it with:
    echo   winget install ffmpeg
    echo Then re-run this file.
    pause
    exit /b 1
)

:: First-time setup
if not exist ".venv" (
    echo First-time setup - this only runs once.
    echo Creating virtual environment...
    uv venv .venv
    echo Installing Python dependencies...
    uv pip install -r requirements.txt --python .venv\Scripts\python
    echo.
)

uv run python -m s4converter.gui
