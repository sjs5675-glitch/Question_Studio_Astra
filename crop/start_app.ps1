[CmdletBinding()]
param(
    [switch]$Preflight,
    [switch]$NoBrowser,
    [switch]$Foreground,
    [switch]$Wait,
    [string]$Workspace,
    [string]$CropRoot,
    [string[]]$ImportRoot,
    [string[]]$PdfRoot,
    [string]$Port
)
$ErrorActionPreference = 'Stop'
$astraRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $astraRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run uv sync in C:/Question_Studio_Astra first.' }
$arguments = @('-B', (Join-Path $astraRoot 'launch_astra.py'), '--component', 'crop')
if ($Preflight) { $arguments += '--preflight' }
foreach ($entry in @(@('--workspace', $Workspace), @('--crop-root', $CropRoot), @('--port', $Port))) {
    if ($null -ne $entry[1] -and $entry[1] -ne '') { $arguments += @($entry[0], $entry[1]) }
}
foreach ($root in $ImportRoot) { $arguments += @('--import-root', $root) }
foreach ($root in $PdfRoot) { $arguments += @('--pdf-root', $root) }
& $python @arguments
exit $LASTEXITCODE