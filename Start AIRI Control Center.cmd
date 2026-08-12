@echo off
setlocal
cd /d "%~dp0"
where pythonw.exe >nul 2>nul
if errorlevel 1 (
  python -m fle.companion.control_center
) else (
  start "" pythonw.exe -m fle.companion.control_center
)
