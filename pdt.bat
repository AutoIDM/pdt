@echo off
rem Run pdt straight from a clone of this repository, with no install step.
rem `uv tool install pdt-cli` gives the same command without this script.
set "PDT_UV=uv"
where uv >nul 2>nul && goto run
set "PDT_UV_INSTALL_DIR=%~dp0.uv"
set "PDT_UV=%PDT_UV_INSTALL_DIR%\uv.exe"
if exist "%PDT_UV%" goto local_path
echo uv is not installed; installing uv 0.12.5 for PDT...
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$env:UV_UNMANAGED_INSTALL=$env:PDT_UV_INSTALL_DIR; irm 'https://astral.sh/uv/0.12.5/install.ps1' | iex" || exit /b 1
if not exist "%PDT_UV%" exit /b 1
:local_path
set "PATH=%PDT_UV_INSTALL_DIR%;%PATH%"
:run
if not defined UV_LINK_MODE set "UV_LINK_MODE=copy"
"%PDT_UV%" run --quiet --project "%~dp0." pdt %*
