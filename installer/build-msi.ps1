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

& $Wix build (Join-Path $PSScriptRoot 'pdt.wxs') -arch x64 -d "Version=$Version" -d "Payload=$payload" -o $Out
if ($LASTEXITCODE) { throw "wix build failed with exit code $LASTEXITCODE" }
& $Wix msi validate $Out
if ($LASTEXITCODE) { throw "wix msi validate failed with exit code $LASTEXITCODE" }
