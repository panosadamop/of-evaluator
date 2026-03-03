@echo off
REM ─────────────────────────────────────────────────────────────
REM Oracle Migrator — setup & run (Windows)
REM Usage: double-click run.bat  OR  run it from a command prompt
REM ─────────────────────────────────────────────────────────────
setlocal

set VENV_DIR=venv

REM ── 1. Find Python ──────────────────────────────────────────
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo ERROR: python not found. Install Python 3.9+ from https://python.org and retry.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version 2^>^&1') do set PY_VERSION=%%i
echo Using %PY_VERSION%

REM ── 2. Create venv if it doesn't exist ──────────────────────
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo Creating virtual environment...
    python -m venv %VENV_DIR%
    echo Virtual environment created at .\%VENV_DIR%
)

REM ── 3. Activate ─────────────────────────────────────────────
call %VENV_DIR%\Scripts\activate.bat
echo Virtual environment activated

REM ── 4. Install / upgrade dependencies ───────────────────────
echo Installing dependencies...
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo Dependencies installed

REM ── 5. Create sample files if missing ───────────────────────
if not exist "sample_files\EMPLOYEE_LOOKUP.fmt" (
    echo Creating sample files...
    python -c "from oracle_migrator.samples import create_samples; create_samples('sample_files')"
)

REM ── 6. Start the app ────────────────────────────────────────
echo.
echo Starting Oracle Migrator...
echo Open http://localhost:5000 in your browser
echo Press Ctrl+C to stop
echo.
python app.py

pause
