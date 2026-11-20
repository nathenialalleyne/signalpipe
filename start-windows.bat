@echo off
rem Double-click to start Signalpipe. The first start sets everything up, which takes a few minutes.
cd /d "%~dp0"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

where uv >nul 2>nul
if errorlevel 1 (
  echo First start: installing uv, which sets up Python for Signalpipe...
  powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
)
where uv >nul 2>nul
if errorlevel 1 (
  echo Could not install uv. Check your internet connection and try again.
  pause
  exit /b 1
)

echo Starting Signalpipe (the first start downloads its packages, a few minutes)...
uv run --quiet --no-project --python 3.13 --with-requirements requirements-local.txt python -m app.local %*
if errorlevel 1 (
  echo Signalpipe stopped with an error.
  pause
)
