<#
    Creates Desktop and Start Menu shortcuts for Practice Helper.

    A real script file rather than PowerShell inlined into install.bat: batch
    escaping of quotes, pipes and parentheses inside a parenthesised block is
    a genuine minefield, and a shortcut that silently fails to appear is a bad
    first impression.
#>
param(
    [Parameter(Mandatory = $true)][string]$Target,   # pythonw.exe
    [Parameter(Mandatory = $true)][string]$AppDir,   # folder holding app.py
    [string]$Name = 'Practice Helper'
)

$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell

$places = @(
    [Environment]::GetFolderPath('Desktop'),
    (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs')
)

foreach ($dir in $places) {
    if (-not (Test-Path $dir)) { continue }
    $lnk = $shell.CreateShortcut((Join-Path $dir ($Name + '.lnk')))
    $lnk.TargetPath       = $Target
    $lnk.Arguments        = '"' + (Join-Path $AppDir 'app.py') + '"'
    $lnk.WorkingDirectory = $AppDir
    $lnk.Description      = $Name
    $icon = Join-Path $AppDir 'assets\icon.ico'
    if (Test-Path $icon) { $lnk.IconLocation = $icon }
    $lnk.Save()
    Write-Host ("  created " + (Join-Path $dir ($Name + '.lnk')))
}
