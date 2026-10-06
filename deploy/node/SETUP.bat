@echo off
rem Unreal Render Farm - render node setup. Double-click to install or update this node.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Asking for administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\setup_node.ps1"
echo.
pause
