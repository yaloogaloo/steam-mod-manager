@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv\Scripts\python.exe. Use your installed SMM Python environment.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -B "launch_smm_next.py" %*
if errorlevel 1 pause
