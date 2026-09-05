<#
    Downloads the Practice Helper source when install.bat was run on its own.
    Kept separate so install.bat never has to escape PowerShell syntax.
#>
param(
    [string]$Repo = 'https://github.com/caleblanc/practice-helper',
    [Parameter(Mandatory = $true)][string]$Dest
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$zip = Join-Path $env:TEMP 'practice-helper.zip'
Invoke-WebRequest -Uri ($Repo + '/archive/refs/heads/main.zip') -OutFile $zip

$stage = Join-Path $env:TEMP 'practice-helper-stage'
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
Expand-Archive -Path $zip -DestinationPath $stage -Force

$inner = (Get-ChildItem -Path $stage -Directory)[0].FullName
if (Test-Path $Dest) { Remove-Item $Dest -Recurse -Force }
Move-Item -Path $inner -Destination $Dest
Remove-Item $zip -Force -ErrorAction SilentlyContinue
Write-Host ("  downloaded to " + $Dest)
