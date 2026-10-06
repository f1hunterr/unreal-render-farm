@echo off
rem Unreal Render Farm - remove the render agent from this machine (files in C:\UnrealRenderFarm are kept).
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\uninstall_service.ps1" -Role Agent
echo.
pause
