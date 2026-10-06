@echo off
rem Unreal Render Farm - set up an artist's PC to help render while nobody is using it.
rem It renders only after 15 minutes without keyboard or mouse, and stops when you come back.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Asking for administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\setup_node.ps1" -Workstation
echo.
pause
