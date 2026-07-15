@echo off
setlocal

set "PROJECT_ROOT=%~dp0.."

if exist "%PROJECT_ROOT%\.venv\Scripts\python.exe" (
  "%PROJECT_ROOT%\.venv\Scripts\python.exe" %*
  exit /b %errorlevel%
)

if exist "%PROJECT_ROOT%\.venv\python.exe" (
  "%PROJECT_ROOT%\.venv\python.exe" %*
  exit /b %errorlevel%
)

where python >nul 2>nul
if errorlevel 1 (
  echo NovelPilot could not find Python. Create .venv or activate a Python 3.13 environment. 1>&2
  exit /b 9009
)

python %*
exit /b %errorlevel%
