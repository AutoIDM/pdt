param(
  [Parameter(Mandatory)][string]$Version,
  [Parameter(Mandatory)][string]$Out
)
$ErrorActionPreference = 'Stop'

$work = Join-Path ([IO.Path]::GetTempPath()) "pdt-launcher-$([guid]::NewGuid())"
New-Item -ItemType Directory -Force $work, (Split-Path $Out) | Out-Null
Set-Content (Join-Path $work 'launcher.rc') "1 ICON `"$(Join-Path $PSScriptRoot 'pdt.ico')`"" -Encoding ascii

$vs = & "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe" -latest -property installationPath
Import-Module (Join-Path $vs 'Common7\Tools\Microsoft.VisualStudio.DevShell.dll')
Enter-VsDevShell -VsInstallPath $vs -SkipAutomaticLocation -DevCmdArguments '-arch=amd64 -no_logo'
& rc /nologo "/fo$work\launcher.res" (Join-Path $work 'launcher.rc')
if ($LASTEXITCODE) { throw "rc failed with exit code $LASTEXITCODE" }
& cl /nologo /O2 /MT /EHsc /W4 /WX "/DPDT_VERSION=$Version" "/Fo$work\launcher.obj" (Join-Path $PSScriptRoot 'launcher.cpp') "$work\launcher.res" /link /SUBSYSTEM:CONSOLE "/OUT:$Out"
if ($LASTEXITCODE) { throw "cl failed with exit code $LASTEXITCODE" }
