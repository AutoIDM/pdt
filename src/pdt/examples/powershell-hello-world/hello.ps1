# Sample PowerShell hello world app. Run it with `pdt run <name>`.
#
# pdt runs every .ps1 file in this folder that no other .ps1 file loads, in
# name order. Replace this file with your own script to make the app do real
# work.

$greeting = 'Hello, world!'
if ($env:PDT_CONFIG_JSON) {
    $config = $env:PDT_CONFIG_JSON | ConvertFrom-Json
    if ($config.greeting) {
        $greeting = $config.greeting
    }
}

Write-Output $greeting
