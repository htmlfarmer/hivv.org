@echo off
REM hivv.cmd - Windows launcher for the HIVV.org client CLI.
REM
REM Downloads the client on first run, then execs python on it.
REM
REM Usage:
REM   hivv --help
REM   hivv start
REM   hivv chat "hello world"
REM
REM Served at https://hivv.org/hivv.cmd and installed by install-hivv.ps1.

setlocal EnableExtensions EnableDelayedExpansion

set "APP=hivv"
set "HUB=https://hivv.org"

REM Resolve install dir from this script's location.
REM %~dp0 ends with a trailing backslash, e.g.  C:\Users\me\.local\bin\
set "SELF_DIR=%~dp0"
set "LIB_DIR=%SELF_DIR%..\lib\%APP%\"
set "CLIENT_FILE=%LIB_DIR%%APP%-cli.py"

REM First-run download (in case user grabbed hivv.cmd without the installer).
if not exist "%CLIENT_FILE%" (
    if not exist "%LIB_DIR%" mkdir "%LIB_DIR%" 2>nul
    echo [%APP%] first run: fetching client from %HUB%/%APP%-cli.py 1>&2
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -Uri '%HUB%/%APP%-cli.py' -OutFile '%CLIENT_FILE%' -UseBasicParsing } catch { exit 1 }"
    if errorlevel 1 (
        echo [%APP%] download failed. Check network or try: 1>&2
        echo   curl -fsSL %HUB%/%APP%-cli.py -o "%CLIENT_FILE%" 1>&2
        exit /b 1
    )
)

REM Pick a Python. Prefer `python` (PATH), then `py -3` (Microsoft Store / launcher).
where python >nul 2>nul
if not errorlevel 1 (
    python "%CLIENT_FILE%" %*
    exit /b %ERRORLEVEL%
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%CLIENT_FILE%" %*
    exit /b %ERRORLEVEL%
)

echo [%APP%] python not found on PATH. Install Python 3: 1>&2
echo   winget install Python.Python.3.12 1>&2
echo   or download from https://python.org/downloads/ 1>&2
exit /b 1
