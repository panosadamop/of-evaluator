@echo off
REM ─────────────────────────────────────────────────────────────
REM Oracle Migrator CLI — venv-aware wrapper (Windows)
REM Usage: migrate.bat <command> [args]
REM   migrate.bat demo
REM   migrate.bat analyze sample_files\
REM   migrate.bat convert sample_files\ --target both --output .\out --zip
REM ─────────────────────────────────────────────────────────────
setlocal

set VENV_DIR=venv

if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo Setting up virtual environment...
    python -m venv %VENV_DIR%
    call %VENV_DIR%\Scripts\activate.bat
    pip install --upgrade pip -q
    pip install -r requirements.txt -q
    echo Setup complete.
) else (
    call %VENV_DIR%\Scripts\activate.bat
)

if not exist "sample_files\EMPLOYEE_LOOKUP.fmt" (
    python -c "from oracle_migrator.samples import create_samples; create_samples('sample_files')" 2>nul
)

python cli.py %*
