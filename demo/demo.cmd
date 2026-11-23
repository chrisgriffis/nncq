@echo off
rem nncq bundled demo: double-click. Indexes demo\orbitkit, starts the server on this folder, opens the dashboard.
rem First run creates ..\.venv (lite: no torch, no GPU, about 150 MB). Close the window to stop the server.
setlocal
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo == first run: installing nncq into %~dp0..\.venv [lite]
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\init.ps1" -Lite -EnvOnly
  if errorlevel 1 goto :fail
)
"%PY%" "%~dp0run.py" %*
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo demo failed: see the messages above
pause
exit /b 1
