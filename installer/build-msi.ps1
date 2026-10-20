param(
  [Parameter(Mandatory)][string]$Wix,
  [Parameter(Mandatory)][string]$Launcher,
  [Parameter(Mandatory)][string]$Version,
  [Parameter(Mandatory)][string]$Out
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$UvVersion = '0.13.1'
$UvSha256 = '3b2661a463a5baea7f5076ea2b25ab71d77f36a6c0d809e5350651792f80a672'

$work = Join-Path ([IO.Path]::GetTempPath()) "pdt-msi-$([guid]::NewGuid())"
$payload = Join-Path $work 'payload'
New-Item -ItemType Directory -Force $payload | Out-Null

$zip = Join-Path $work 'uv.zip'
Invoke-WebRequest "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip
$hash = (Get-FileHash $zip -Algorithm SHA256).Hash
if ($hash -ne $UvSha256) { throw "uv $UvVersion zip has SHA256 $hash, not $UvSha256" }
Expand-Archive $zip (Join-Path $work 'uv')
Copy-Item (Join-Path $work 'uv\uv.exe') $payload
# uv's MIT license requires every copy of uv.exe to carry its license text.
Invoke-WebRequest "https://raw.githubusercontent.com/astral-sh/uv/$UvVersion/LICENSE-MIT" -OutFile (Join-Path $payload 'uv-LICENSE-MIT.txt')
Copy-Item $Launcher (Join-Path $payload 'pdt.exe')

$vs = & "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe" -latest -property installationPath
Import-Module (Join-Path $vs 'Common7\Tools\Microsoft.VisualStudio.DevShell.dll')
Enter-VsDevShell -VsInstallPath $vs -SkipAutomaticLocation -DevCmdArguments '-arch=amd64 -no_logo'
$setup = Join-Path $work 'setup.dll'
& cl /nologo /O2 /MT /LD /EHsc /W4 /WX "/Fo$work\setup.obj" (Join-Path $PSScriptRoot 'setup.cpp') /link "/OUT:$setup" msi.lib
if ($LASTEXITCODE) { throw "cl failed with exit code $LASTEXITCODE" }

$ui = Join-Path (Split-Path $Wix) 'WixToolset.UI.wixext.dll'
$util = Join-Path (Split-Path $Wix) 'WixToolset.Util.wixext.dll'
& $Wix build (Join-Path $PSScriptRoot 'pdt.wxs') -arch x64 -ext $ui -ext $util -d "Version=$Version" -d "Payload=$payload" -d "SetupDll=$setup" -o $Out
if ($LASTEXITCODE) { throw "wix build failed with exit code $LASTEXITCODE" }
& $Wix msi validate $Out
if ($LASTEXITCODE) { throw "wix msi validate failed with exit code $LASTEXITCODE" }
