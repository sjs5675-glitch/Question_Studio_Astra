param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$runtime = Join-Path $PSScriptRoot '.runtime/t12s-cf76f8c9/runtime'
$env:ASTRA_RUNTIME = $runtime
$env:ASTRA_PORTS_FILE = Join-Path $PSScriptRoot '.runtime/t12s-cf76f8c9/ports.json'
& (Join-Path $PSScriptRoot 'studio/start_app.ps1') -OwnerId preview11 -Workspace (Join-Path $PSScriptRoot '.runtime/t12s-cf76f8c9/workspace') -CheckOnly:$CheckOnly
exit $LASTEXITCODE
