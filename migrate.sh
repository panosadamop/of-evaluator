#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Oracle Migrator CLI — venv-aware wrapper
# Usage: ./migrate.sh <command> [args]
#   ./migrate.sh demo
#   ./migrate.sh analyze sample_files/
#   ./migrate.sh convert sample_files/ --target both --output ./out --zip
#   ./migrate.sh pipeline sample_files/ --output ./out
# ─────────────────────────────────────────────────────────────
set -e

VENV_DIR="venv"

# Bootstrap venv if not present
if [ ! -d "$VENV_DIR" ]; then
    echo "Setting up virtual environment..."
    python3 -m venv "$VENV_DIR"
    source "$VENV_DIR/bin/activate"
    pip install --upgrade pip -q
    pip install -r requirements.txt -q
    echo "Setup complete."
else
    source "$VENV_DIR/bin/activate"
fi

# Create sample files if missing
if [ ! -d "sample_files" ] || [ -z "$(ls -A sample_files 2>/dev/null)" ]; then
    python -c "from oracle_migrator.samples import create_samples; create_samples('sample_files')" 2>/dev/null || true
fi

python cli.py "$@"
