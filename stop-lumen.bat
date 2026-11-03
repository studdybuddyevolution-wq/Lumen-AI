@echo off
rem ============================================================
rem  LUMEN — Clean Shutdown Script
rem  Gracefully terminates all Lumen services:
rem    - Backtalk Voice Runtime (ports 8791, 8796)
rem    - Dashboard Web Server (port 8795)
rem    - Barehands Gesture Server (port 8794)
rem ============================================================
title Lumen Shutdown

echo.
echo   +-----------------------------------------------------------+
echo   ^|                LUMEN - CLEAN SHUTDOWN                     ^|
echo   +-----------------------------------------------------------+
echo.

rem Stop Voice line (port 8791) and Bridge (port 8796)
echo   [1/3] Stopping Lumen Voice Runtime and Bridge...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8791 " ^| findstr "LISTENING"') do (
    if not "%%a"=="0" if not "%%a"=="" (
        taskkill /PID %%a /F >nul 2>nul
        echo   [OK] Voice process PID %%a stopped.
    )
)
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8796 " ^| findstr "LISTENING"') do (
    if not "%%a"=="0" if not "%%a"=="" (
        taskkill /PID %%a /F >nul 2>nul
        echo   [OK] Bridge process PID %%a stopped.
    )
)

rem Stop Dashboard (port 8795)
echo   [2/3] Stopping Dashboard Web Server...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8795 " ^| findstr "LISTENING"') do (
    if not "%%a"=="0" if not "%%a"=="" (
        taskkill /PID %%a /F >nul 2>nul
        echo   [OK] Dashboard process PID %%a stopped.
    )
)

rem Stop Barehands (port 8794)
echo   [3/3] Stopping Barehands Server...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8794 " ^| findstr "LISTENING"') do (
    if not "%%a"=="0" if not "%%a"=="" (
        taskkill /PID %%a /F >nul 2>nul
        echo   [OK] Barehands process PID %%a stopped.
    )
)

rem Close any minimized support windows if still open
taskkill /FI "WINDOWTITLE eq Lumen Barehands" /F >nul 2>nul
taskkill /FI "WINDOWTITLE eq Lumen Dashboard" /F >nul 2>nul

echo.
echo   [COMPLETE] All Lumen services have been shut down cleanly.
echo.
timeout /t 3 /nobreak >nul 2>nul || ping -n 4 127.0.0.1 >nul
