# More ways to install

The [Quick Start](../README.md#quick-start) covers the usual way. These are the others.

## With uv

```
uv tool install pdt-cli
uv tool update-shell
```

`uv tool install` puts the `pdt` command in uv's tool folder. `uv tool update-shell` adds that folder to your PATH if it is not there yet. A terminal window that was already open does not see a PATH change, so open a new window before you type `pdt`. To update pdt later, run `uv tool upgrade pdt-cli`.

## Windows, from PowerShell

Paste these lines into PowerShell. They install `uv` and pdt, and they work in the window you type them in:

```powershell
winget install --id astral-sh.uv -e
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
uv tool install pdt-cli
uv tool update-shell
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
pdt --version
```

`winget` and `uv tool update-shell` change the PATH for new windows only. Each `$env:Path = ...` line reads the new PATH into the current window. Another PowerShell window that was open before the install does not find `uv` or `pdt` until you paste the same line into it or open a new window.

## The Windows installer

The [pdt installer](https://github.com/AutoIDM/pdt/releases/latest/download/pdt-windows-x64.msi) installs pdt for your user only, so it does not ask for administrator rights. It installs `pdt` and `uv`, adds `pdt` to your PATH, and sets up Python 3.12 and pdt. Then open a new PowerShell window and run `pdt init`. If the computer had no network during the install, the first `pdt` command finishes the setup. To remove pdt, open **Settings > Apps > Installed apps** and uninstall pdt. This removes the pdt files in `%LOCALAPPDATA%\AutoIDM\pdt` but keeps the Python that uv installed.

## From a clone

Clone this repository and run `./pdt` (or `.\pdt.bat` on Windows). It installs `uv` for you if you do not have it. Every way gives you the same commands.
