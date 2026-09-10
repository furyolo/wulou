@echo off
setlocal
set "PROJECT_ROOT=%~dp0.."
where uv >nul 2>nul
if errorlevel 1 (
  echo uv is not installed. Please install uv and try again.
  pause
  exit /b 1
)
uv run --project "%PROJECT_ROOT%" python "%PROJECT_ROOT%\server\main.py"
if errorlevel 1 pause
