[CmdletBinding()]
param([string]$OwnerId = 'studio')
$ErrorActionPreference = 'Stop'
$astraRoot = Split-Path -Parent $PSScriptRoot
& (Join-Path $astraRoot '.venv/Scripts/python.exe') -B (Join-Path $astraRoot 'launch_astra.py') --component studio --stop-owned --owner-id $OwnerId
exit $LASTEXITCODE
