@echo off
rem Launcher stub. All messages live in launch.py so they can be in Persian:
rem cmd cannot read non-ASCII text inside a .bat file, but Python prints
rem Unicode to the Windows console correctly.
cd /d "%~dp0"
set "PY=python"
where python >nul 2>nul || set "PY=py"
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install it from python.org
  echo and tick "Add python.exe to PATH" during setup.
  echo.
  pause
  exit /b 1
)
%PY% launch.py
echo.
pause
