#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Oracle Migrator — setup & run (Linux / macOS)
# Usage:
#   chmod +x run.sh
#   ./run.sh           # first run: creates venv, installs deps, starts app
#   ./run.sh           # subsequent runs: activates venv, starts app
# ─────────────────────────────────────────────────────────────
set -e

VENV_DIR="venv"
PYTHON="python3"

# ── 1. Find Python ────────────────────────────────────────────
if ! command -v "$PYTHON" &>/dev/null; then
    echo "ERROR: python3 not found. Install Python 3.9+ and retry."
    exit 1
fi

PY_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "Using Python $PY_VERSION"

# ── 2. Create venv if it doesn't exist ───────────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
    echo "Virtual environment created at ./$VENV_DIR"
fi

# ── 3. Activate ──────────────────────────────────────────────
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
echo "Virtual environment activated"

# ── 4. Install / upgrade dependencies ────────────────────────
echo "Installing dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "Dependencies installed"

# ── 5. Create sample files if missing ────────────────────────
if [ ! -d "sample_files" ] || [ -z "$(ls -A sample_files 2>/dev/null)" ]; then
    echo "Creating sample files..."
    python -c "from oracle_migrator.samples import create_samples; create_samples('sample_files')"
fi

# ── 6. Start the app ─────────────────────────────────────────
echo ""
echo "Starting Oracle Migrator..."
echo "Open http://localhost:5000 in your browser"
echo "Press Ctrl+C to stop"
echo ""
python app.py
