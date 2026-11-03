@echo off
rem ============================================================
rem  LUMEN — One-Click Launcher
rem  Starts:
rem    1. Ollama & Qwen model verification
rem    2. Barehands Gesture Server (port 8794)
rem    3. Lumen Dashboard Server (port 8795)
rem    4. Dashboard in Chrome (http://127.0.0.1:8795)
rem    5. Backtalk Voice Runtime & Bridge (ports 8791, 8796)
rem ============================================================
title Lumen Assistant Launcher

cd /d "%~dp0"

if exist "%USERPROFILE%\.local\bin\uv.exe" (
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

echo.
echo   +-----------------------------------------------------------+
echo   ^|                LUMEN - LOCAL AI ASSISTANT                 ^|
echo   ^|         Starting Voice, Memory, Dashboard ^& Gestures      ^|
echo   +-----------------------------------------------------------+
echo.

rem ---- Step 1: Verify Ollama ----
echo   [1/5] Checking local Ollama service...
curl -s -o nul -w "%%{http_code}" http://127.0.0.1:11434/api/tags > "%TEMP%\lumen_ollama.tmp" 2>nul
set /p OLLAMA_CODE=<"%TEMP%\lumen_ollama.tmp"
del "%TEMP%\lumen_ollama.tmp" 2>nul

if "%OLLAMA_CODE%"=="200" (
    echo   [OK]   Ollama is active on http://127.0.0.1:11434
) else (
    echo   [WARN] Ollama is not responding at http://127.0.0.1:11434
    echo          Please make sure the Ollama application is running.
    echo          Attempting to continue...
)

rem ---- Step 2: Verify Qwen 3.5 4B model ----
echo   [2/5] Checking Qwen 3.5 4B model in Ollama...
curl -s http://127.0.0.1:11434/api/tags 2>nul | findstr /i "qwen" >nul 2>nul
if %ERRORLEVEL% equ 0 (
    echo   [OK]   Qwen model installed and verified.
) else (
    echo   [WARN] Qwen model not detected in Ollama tags.
    echo          If not installed yet, run: ollama run qwen3.5:4b
)

rem ---- Step 3: Start Barehands (port 8794) ----
echo   [3/5] Starting Barehands Gesture Server...
if exist "barehands\run.bat" (
    netstat -an 2>nul | findstr ":8794 " | findstr "LISTENING" >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        echo   [OK]   Barehands already running on port 8794.
    ) else (
        start "Lumen Barehands" /min cmd /c "cd /d "%~dp0barehands" && run.bat"
        echo   [OK]   Barehands server started on port 8794.
    )
) else (
    echo   [INFO] Barehands not found in barehands directory.
)

rem ---- Step 4: Start Dashboard Server (port 8795) ----
echo   [4/5] Starting Lumen Dashboard Server...
if exist "dashboard\server.py" (
    netstat -an 2>nul | findstr ":8795 " | findstr "LISTENING" >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        echo   [OK]   Dashboard server already running on port 8795.
    ) else (
        set "DASH_PY=python"
        if exist "%~dp0backtalk\.venv\Scripts\python.exe" (
            set "DASH_PY=%~dp0backtalk\.venv\Scripts\python.exe"
        )
        start "Lumen Dashboard" /min cmd /c "cd /d "%~dp0dashboard" && "%DASH_PY%" server.py"
        echo   [OK]   Dashboard server started on port 8795.
    )
) else (
    echo   [WARN] dashboard\server.py not found.
)

rem ---- Wait 2 seconds for servers to initialize ----
timeout /t 2 /nobreak >nul

rem ---- Open Dashboard in Chrome ----
echo   [+]   Opening Lumen Dashboard in browser...
start "" "chrome.exe" "http://127.0.0.1:8795" 2>nul
if %ERRORLEVEL% neq 0 (
    start "" "http://127.0.0.1:8795"
)

rem ---- Step 5: Start Voice Line & Shared WarmBrain in this window ----
echo   [5/5] Starting Voice Line & WarmBrain Bridge...
if exist "backtalk\pyproject.toml" (
    echo.
    echo   -------------------------------------------------------------
    echo     Lumen voice runtime is running in this window.
    echo     - Press and hold the [Home] key to speak to Lumen.
    echo     - Or use the typed chat in the Dashboard browser tab.
    echo     - Close this window or run stop-lumen.bat to shut down.
    echo   -------------------------------------------------------------
    echo.
    cd /d "%~dp0backtalk"
    where uv >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        uv sync --inexact
        if %ERRORLEVEL% neq 0 (
            echo.
            echo   [ERROR] uv sync failed.
            pause
            exit /b 1
        )
        uv run python -m backtalk.main
    ) else if exist "%~dp0backtalk\.venv\Scripts\python.exe" (
        "%~dp0backtalk\.venv\Scripts\python.exe" -m backtalk.main
    ) else (
        python -m backtalk.main
    )
    if %ERRORLEVEL% neq 0 (
        echo.
        echo   [ERROR] Voice line terminated with an error.
        echo   Check backtalk\logs\backtalk.log for details.
        pause
    )
) else (
    echo   [INFO] Backtalk not found. Dashboard is active at http://127.0.0.1:8795
    pause
)
