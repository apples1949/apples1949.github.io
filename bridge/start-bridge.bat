@echo off
cd /d "%~dp0"
title L4D2 A2S Bridge
where python >nul 2>nul
if %errorlevel%==0 (
    python l4d2_a2s_bridge.py %*
) else (
    py -3 l4d2_a2s_bridge.py %*
)
echo.
echo Bridge stopped. Press any key to close.
pause >nul
