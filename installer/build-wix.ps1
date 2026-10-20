# The WiX binary release comes with the OSMF EULA, which covers only that binary release;
# its section 4 lets a user compile WiX from the MS-RL source without it. The source still
# refuses to run until the EULA is accepted, so this build removes each check and each
# acceptance that runs while WiX builds (MS-RL allows the change). It builds no WiX test
# project: EulaFixture expects the check, and CompileCoreTestExtensionWixlib passes -acceptEula.
param([Parameter(Mandatory)][string]$Out)
$ErrorActionPreference = 'Stop'

$Tag = 'v7.0.0'
$Commit = 'b8977d6f88e7b68e000bac226a2814f236770570'

function Invoke-Native {
  & $args[0] $args[1..($args.Count - 1)]
  if ($LASTEXITCODE) { throw "$args failed with exit code $LASTEXITCODE" }
}

$Out = [IO.Path]::GetFullPath($Out)
$wix = Join-Path $Out 'src'
Invoke-Native git clone --quiet --depth 1 --branch $Tag https://github.com/wixtoolset/wix.git $wix
$head = git -C $wix rev-parse HEAD
if ($head -ne $Commit) { throw "WiX $Tag is at $head, not $Commit" }

$patches = @(
  @('src\wix\WixToolset.Core\CommandLine\CommandLine.cs', '(?s)(private bool RequiresEulaAcceptance\(ICommandLineCommand command\)\s*\{\s*)return [^;]*;', '${1}return false;'),
  @('src\Directory.csproj.props', '<AcceptEula>[^<]*</AcceptEula>', ''),
  @('src\Directory.wixproj.targets', '<AcceptEula>[^<]*</AcceptEula>', ''),
  @('src\internal\targets\CheckLicenseAcceptance.targets', '<Error\s[^>]*Code="WIX7015"[^>]*/>', ''),
  @('src\wix\WixToolset.Sdk\tools\wix.targets', '<Error\s[^>]*Code="WIX7015"[^>]*/>', '')
)
foreach ($p in $patches) {
  $file = Join-Path $wix $p[0]
  $text = [IO.File]::ReadAllText($file)
  if ([regex]::Matches($text, $p[1]).Count -ne 1) { throw "The EULA acceptance in $file changed; update this script" }
  [IO.File]::WriteAllText($file, [regex]::Replace($text, $p[1], $p[2]))
}

$vs = & "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe" -latest -property installationPath
Import-Module (Join-Path $vs 'Common7\Tools\Microsoft.VisualStudio.DevShell.dll')
Enter-VsDevShell -VsInstallPath $vs -SkipAutomaticLocation -DevCmdArguments '-arch=amd64 -no_logo'

$env:NUGET_PACKAGES = Join-Path $Out 'nuget'
# WiX audits every package at every severity, and -warnaserror turns an advisory published
# after the pinned tag (such as NU1902) into a build error that the pin cannot fix.
# nuget.exe restore ignores NuGetAudit=false, so -nowarn keeps its audit results out of the build.
$c = '-p:Configuration=Release;NuGetAudit=false'
$n = '-nowarn:NU1900;NU1901;NU1902;NU1903;NU1904'
Push-Location (Join-Path $wix 'src')
try {
  New-Item -ItemType Directory -Force ..\build\artifacts, ..\build\logs | Out-Null
  Invoke-Native msbuild -Restore internal\SetBuildNumber\SomeVerInit.verproj -nologo
  Push-Location dtf
  Invoke-Native msbuild -Restore SfxCA\sfxca_t.proj $c $n -nologo -m -warnaserror
  Invoke-Native msbuild -Restore '-t:Pack' dtf.slnx $c $n -nologo -m -warnaserror
  Pop-Location
  Push-Location internal
  Invoke-Native nuget restore
  Invoke-Native msbuild internal_t.proj $c $n -nologo -warnaserror
  Pop-Location
  Push-Location libs
  Invoke-Native msbuild -Restore libs_t.proj $c $n -nologo -m -warnaserror
  Pop-Location
  Push-Location api
  Invoke-Native msbuild api_t.proj $c $n -nologo -warnaserror
  Pop-Location
  Push-Location burn
  Invoke-Native nuget restore
  Invoke-Native msbuild burn_t.proj $c $n -nologo -m -warnaserror
  Pop-Location
  Push-Location wix
  Invoke-Native msbuild '-t:Restore' wix.slnx $c $n -nologo -m -warnaserror
  Invoke-Native msbuild wixnative\wixnative_t.proj $c $n -nologo -m -warnaserror
  Invoke-Native msbuild publish_t.proj $c $n -nologo -warnaserror
  Invoke-Native msbuild '-t:Publish;Pack' WixToolset.Sdk\WixToolset.Sdk.csproj $c $n -nologo -warnaserror
  Pop-Location
  Push-Location ext\UI\wixext
  Invoke-Native msbuild -Restore WixToolset.UI.wixext.csproj $c $n -nologo -warnaserror
  Pop-Location
  Push-Location ext\Util\wixext
  Invoke-Native msbuild -Restore WixToolset.Util.wixext.csproj $c $n -nologo -warnaserror
  Pop-Location
} finally {
  Pop-Location
}

New-Item -ItemType Directory -Force (Join-Path $Out 'bin') | Out-Null
Copy-Item (Join-Path $wix 'build\wix\Release\publish\wix\*') (Join-Path $Out 'bin') -Recurse -Force
Copy-Item (Join-Path $wix 'build\UI.wixext\Release\netstandard2.0\WixToolset.UI.wixext.dll') (Join-Path $Out 'bin')
Copy-Item (Join-Path $wix 'build\Util.wixext\Release\netstandard2.0\WixToolset.Util.wixext.dll') (Join-Path $Out 'bin')
Invoke-Native (Join-Path $Out 'bin\wix.exe') --version
