#Requires -Version 7.0
# Lists facts about the PowerShell files in a folder as JSON. It judges
# nothing; pdt/powershell.py holds the rules, so they run without pwsh in tests.
# The JSON goes to $Out, not stdout: Get-Command can load a module, and whatever
# that module prints while it loads (ImportExcel warns when libgdiplus is
# missing) would land on stdout ahead of the JSON.
using namespace System.Management.Automation.Language
param([Parameter(Mandatory)][string]$Folder, [Parameter(Mandatory)][string]$Out)

function Get-ArgText($cmdAst, $i) {
  $el = $cmdAst.CommandElements[$i]
  if ($null -eq $el -or $el -is [CommandParameterAst]) { return $null }
  if ($el -is [StringConstantExpressionAst] -or $el -is [ExpandableStringExpressionAst]) { return $el.Value }
  return $el.Extent.Text
}

function Get-Arg($cmdAst, [string]$ParamName, [int]$Position = -1) {
  $els = $cmdAst.CommandElements
  $pos = 0
  for ($i = 1; $i -lt $els.Count; $i++) {
    if ($els[$i] -is [CommandParameterAst]) {
      if ($els[$i].ParameterName -ieq $ParamName) {
        if ($els[$i].Argument) { return $els[$i].Argument.Extent.Text }
        return Get-ArgText $cmdAst ($i + 1)
      }
      if ($null -eq $els[$i].Argument -and $i + 1 -lt $els.Count -and $els[$i + 1] -isnot [CommandParameterAst]) { $i++ }
      continue
    }
    if ($pos -eq $Position) { return Get-ArgText $cmdAst $i }
    $pos++
  }
  return $null
}

function Get-NameList($text) {
  if ($null -eq $text) { return @() }
  return @($text -split ',' | ForEach-Object { $_.Trim().Trim('"', "'") } | Where-Object { $_ -ne '' })
}

function Get-ModuleSpec($spec) {
  if ($spec -is [string]) { return [ordered]@{ name = $spec } }
  return [ordered]@{
    name = $spec.Name; requiredVersion = $spec.RequiredVersion ? $spec.RequiredVersion.ToString() : $null
    version = $spec.Version ? $spec.Version.ToString() : $null
    maximumVersion = $spec.MaximumVersion ? $spec.MaximumVersion.ToString() : $null
  }
}

function Get-Snippet($ast) {
  $t = $ast.Extent.Text
  return $t.Substring(0, [Math]::Min(80, $t.Length))
}

function Read-ScriptFile([string]$Path, [string]$Folder) {
  $tokens = $null; $errors = $null
  $ast = [Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
  $f = [ordered]@{
    file = [IO.Path]::GetRelativePath($Folder, $Path).Replace('\', '/')
    parseErrors = @($errors | ForEach-Object { [ordered]@{ line = $_.Extent.StartLineNumber; message = $_.Message } })
    requires = $null; usingModules = @(); importModules = @(); commands = @(); definedFunctions = @()
    params = @(); localInvocations = @(); strings = @(); types = @(); newObjects = @(); assemblies = @()
    dynamic = @(); remoting = @(); envReads = @()
  }
  $req = $ast.ScriptRequirements
  if ($req) {
    $f.requires = [ordered]@{
      version = $req.RequiredPSVersion ? $req.RequiredPSVersion.ToString() : $null
      editions = @($req.RequiredPSEditions)
      modules = @($req.RequiredModules | ForEach-Object { Get-ModuleSpec $_ })
    }
  }
  $f.usingModules = @($ast.FindAll({ $args[0] -is [UsingStatementAst] -and $args[0].UsingStatementKind -eq 'Module' }, $true) | ForEach-Object { $_.Name.Value })
  $f.definedFunctions = @($ast.FindAll({ $args[0] -is [FunctionDefinitionAst] }, $true) | ForEach-Object { $_.Name })
  if ($ast.ParamBlock) {
    foreach ($p in $ast.ParamBlock.Parameters) {
      $mandatory = $false
      foreach ($a in $p.Attributes) {
        if ($a -isnot [AttributeAst] -or $a.TypeName.Name -ine 'Parameter') { continue }
        foreach ($na in $a.NamedArguments) {
          if ($na.ArgumentName -ieq 'Mandatory' -and ($na.ExpressionOmitted -or $na.Argument.Extent.Text -match '^\$true$|^1$')) { $mandatory = $true }
        }
      }
      $f.params += [ordered]@{ name = $p.Name.VariablePath.UserPath; mandatory = $mandatory; hasDefault = ($null -ne $p.DefaultValue); line = $p.Extent.StartLineNumber }
    }
  }
  foreach ($c in $ast.FindAll({ $args[0] -is [CommandAst] }, $true)) {
    $line = $c.Extent.StartLineNumber
    $name = $c.GetCommandName()
    if ($null -eq $name -and $c.CommandElements[0] -is [ExpandableStringExpressionAst]) { $name = $c.CommandElements[0].Value }
    if ($null -eq $name) {
      $f.dynamic += [ordered]@{ kind = 'command-from-expression'; text = (Get-Snippet $c); line = $line }
      continue
    }
    $how = $c.InvocationOperator.ToString()
    $pathLike = $name -match '[\\/]|\.ps1$|\.psm1$|\.psd1$'
    if ($how -eq 'Dot' -or ($how -eq 'Ampersand' -and $pathLike) -or ($how -eq 'Unknown' -and $pathLike)) {
      $f.localInvocations += [ordered]@{ how = $how; target = $name; line = $line }
      continue
    }
    $named = @($c.CommandElements | Where-Object { $_ -is [CommandParameterAst] } | ForEach-Object { $_.ParameterName })
    $splatted = [bool]($c.CommandElements | Where-Object { $_ -is [VariableExpressionAst] -and $_.Splatted })
    $f.commands += [ordered]@{ name = $name; line = $line; parameters = $named; splatted = $splatted }
    if ($name -in 'Import-Module', 'ipmo') {
      foreach ($m in (Get-NameList (Get-Arg $c 'Name' 0))) {
        $f.importModules += [ordered]@{
          name = $m; requiredVersion = Get-Arg $c 'RequiredVersion'; minimumVersion = Get-Arg $c 'MinimumVersion'
          maximumVersion = Get-Arg $c 'MaximumVersion'; line = $line
        }
        if ($m -match '[\\/]|\.psm1$|\.psd1$') { $f.localInvocations += [ordered]@{ how = 'Import-Module'; target = $m; line = $line } }
      }
    }
    if ($name -in 'Invoke-Expression', 'iex') { $f.dynamic += [ordered]@{ kind = 'Invoke-Expression'; text = (Get-Snippet $c); line = $line } }
    if ($name -in 'Invoke-Command', 'icm' -and ($named -contains 'ComputerName' -or $named -contains 'Session')) {
      $f.remoting += [ordered]@{ text = (Get-Snippet $c); line = $line }
    }
    if ($name -eq 'New-Object') {
      $f.newObjects += [ordered]@{ typeName = (Get-Arg $c 'TypeName' 0); comObject = (Get-Arg $c 'ComObject'); line = $line }
    }
    if ($name -eq 'Add-Type') {
      foreach ($a in (Get-NameList (Get-Arg $c 'AssemblyName'))) { $f.assemblies += [ordered]@{ name = $a; line = $line } }
    }
  }
  foreach ($v in $ast.FindAll({ $args[0] -is [VariableExpressionAst] -and $args[0].VariablePath.DriveName -eq 'env' }, $true)) {
    $set = $v.Parent -is [AssignmentStatementAst] -and $v.Parent.Left -eq $v
    $f.envReads += [ordered]@{ name = $v.VariablePath.UserPath.Substring(4); line = $v.Extent.StartLineNumber; set = $set }
  }
  foreach ($m in $ast.FindAll({ $args[0] -is [InvokeMemberExpressionAst] }, $true)) {
    if ($m.Member.Extent.Text -in 'Invoke', 'InvokeScript') { $f.dynamic += [ordered]@{ kind = '.Invoke()'; text = (Get-Snippet $m); line = $m.Extent.StartLineNumber } }
  }
  foreach ($t in $ast.FindAll({ $args[0] -is [TypeExpressionAst] -or $args[0] -is [ConvertExpressionAst] }, $true)) {
    $typeName = ($t -is [ConvertExpressionAst]) ? $t.Type.TypeName : $t.TypeName
    $f.types += [ordered]@{ name = $typeName.FullName; line = $t.Extent.StartLineNumber }
  }
  foreach ($s in $ast.FindAll({ $args[0] -is [StringConstantExpressionAst] -or $args[0] -is [ExpandableStringExpressionAst] }, $true)) {
    $v = $s.Value
    if ($null -eq $v) { continue }
    $kind = $null
    if ($v -match '^(HKLM|HKCU|HKCR|HKU|HKCC):' -or $v -match 'Registry::') { $kind = 'registry' }
    elseif ($v -match '^[A-Za-z]:\\') { $kind = 'windows-path' }
    elseif ($v -match '^\\\\[^\\]+\\') { $kind = 'unc-path' }
    elseif ($v -match '^Cert:') { $kind = 'cert-drive' }
    if ($kind) { $f.strings += [ordered]@{ kind = $kind; text = $v; line = $s.Extent.StartLineNumber } }
  }
  return $f
}

function Read-Requirements([string]$Path) {
  $data = Import-PowerShellDataFile -LiteralPath $Path
  $out = [ordered]@{}
  foreach ($key in $data.Keys) {
    $v = $data[$key]
    if ($v -is [hashtable]) {
      $spec = [ordered]@{}
      foreach ($k in $v.Keys) { $spec[[string]$k] = [string]$v[$k] }
      $out[[string]$key] = $spec
    }
    else { $out[[string]$key] = [string]$v }
  }
  return $out
}

$Folder = (Resolve-Path -LiteralPath $Folder).Path
$files = @(Get-ChildItem -LiteralPath $Folder -Recurse -File -Include *.ps1, *.psm1, *.psd1 | Sort-Object FullName)
$scans = @($files | Where-Object { $_.Name -ne 'requirements.psd1' } | ForEach-Object { Read-ScriptFile $_.FullName $Folder })
$requirements = $null
$requirementsFile = Join-Path $Folder 'requirements.psd1'
if (Test-Path -LiteralPath $requirementsFile) { $requirements = Read-Requirements $requirementsFile }
$names = @($scans | ForEach-Object { $_.commands } | ForEach-Object { $_.name } | Sort-Object -Unique)
$modules = @{}
foreach ($found in @(Get-Command -Name $names -ErrorAction SilentlyContinue)) {
  $target = $found
  if ($found.CommandType -eq 'Alias' -and $found.ResolvedCommand) { $target = $found.ResolvedCommand }
  $modules[$found.Name.ToLowerInvariant()] = [string]$target.ModuleName
}
$known = [ordered]@{}
foreach ($n in $names) { $known[$n] = $modules[$n.ToLowerInvariant()] }
[ordered]@{ files = $scans; requirements = $requirements; known = $known } | ConvertTo-Json -Depth 8 |
  Set-Content -LiteralPath $Out -Encoding utf8NoBOM
