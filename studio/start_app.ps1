[CmdletBinding()]
param(
    [switch]$Preflight,
    [switch]$NoBrowser,
    [switch]$Foreground,
    [switch]$Wait,
    [switch]$CheckOnly,
    [string]$Workspace,
    [string]$CropRoot,
    [string[]]$ImportRoot,
    [string[]]$PdfRoot,
    [string]$Port,
    [string]$OwnerId = 'studio'
)
$ErrorActionPreference = 'Stop'
$astraRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $astraRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run uv sync in C:/Question_Studio_Astra first.' }
$arguments = @('-B', (Join-Path $astraRoot 'launch_astra.py'), '--component', 'studio')
$arguments += @('--owned', '--owner-id', $OwnerId)
if ($Preflight) { $arguments += '--preflight' }
foreach ($entry in @(@('--workspace', $Workspace), @('--crop-root', $CropRoot), @('--port', $Port))) {
    if ($null -ne $entry[1] -and $entry[1] -ne '') { $arguments += @($entry[0], $entry[1]) }
}
foreach ($root in $ImportRoot) { $arguments += @('--import-root', $root) }
foreach ($root in $PdfRoot) { $arguments += @('--pdf-root', $root) }
if ($Preflight -or $NoBrowser -or $Foreground -or $Wait) {
    & $python @arguments
    exit $LASTEXITCODE
}
function Resolve-AstraPath([string]$Value) {
    if (-not $Value -or $Value -ne $Value.Trim() -or $Value -match '[\x00-\x1f]' -or $Value.StartsWith('\\')) { throw 'Invalid Astra path.' }
    $resolved = [IO.Path]::GetFullPath([IO.Path]::Combine($astraRoot, $Value))
    if (-not $resolved.StartsWith($astraRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or $resolved.Substring(2).Contains(':')) { throw 'Path must remain inside Astra.' }
    $cursor = $resolved
    while ($cursor -and $cursor.Length -ge $astraRoot.Length) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked Astra paths are not allowed.' }
        }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
    return $resolved
}
if ($OwnerId -notmatch '^[a-z0-9-]{1,64}$') { throw 'Invalid Astra owner.' }
function Read-AstraReceipt([string]$Path) {
    $safePath = Resolve-AstraPath $Path
    $stream = [IO.File]::Open($safePath, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
    try {
        $reader = [IO.StreamReader]::new($stream, [Text.Encoding]::UTF8)
        try { $content = $reader.ReadToEnd() } finally { $reader.Dispose() }
    } finally { $stream.Dispose() }
    return $content | ConvertFrom-Json
}
$runtimeValue = if ($env:ASTRA_RUNTIME) { $env:ASTRA_RUNTIME } else { '.runtime' }
$workspaceValue = if ($Workspace) { $Workspace } elseif ($env:ASTRA_WORKSPACE) { $env:ASTRA_WORKSPACE } else { 'studio/workspace' }
$runtime = Resolve-AstraPath $runtimeValue
$expectedWorkspace = Resolve-AstraPath $workspaceValue
$venvHome = (Get-Content -LiteralPath (Join-Path $astraRoot '.venv/pyvenv.cfg') | Where-Object { $_ -match '^home = ' } | Select-Object -First 1) -replace '^home = ', ''
$basePython = Resolve-AstraPath (Join-Path $venvHome 'python.exe')
$owner = Resolve-AstraPath (Join-Path $runtime ('owned-studio/' + $OwnerId))
$statePath = Join-Path $owner 'owner.json'
$accessPath = Join-Path $owner 'access.json'
$leasePath = Join-Path $owner 'lease.lock'
function Read-OwnedState {
    $state = Read-AstraReceipt $statePath
    if ($state.owner_id -ne $OwnerId -or (Resolve-AstraPath $state.workspace) -ne $expectedWorkspace -or (Resolve-AstraPath $state.python) -ne $python) { throw 'Astra owner or workspace does not match.' }
    if ($state.status -ne 'running' -and $state.status -ne 'starting') { throw 'Astra owner is not running; retained state requires inspection.' }
    $instance = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + [int]$state.pid)
    if (-not $instance) { throw 'Stale Astra process; no process was stopped.' }
    if ($instance.ExecutablePath -and $instance.ExecutablePath -notin @($python, $basePython)) { throw 'Foreign Astra executable; no process was stopped.' }
    if ($instance.CommandLine -and ($instance.CommandLine -notmatch [regex]::Escape((Join-Path $astraRoot 'launch_astra.py')) -or $instance.CommandLine -notmatch ('--owner-id"?\s+"?' + [regex]::Escape($OwnerId) + '(?:"|\s|$)'))) { throw 'Foreign Astra command; no process was stopped.' }
    if (-not $instance.ExecutablePath -or -not $instance.CommandLine) {
        $listener = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort ([int]$state.port) -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.OwningProcess -eq [int]$state.pid })
        if ($instance.Name -ne 'python.exe' -or $listener.Count -ne 1) { throw 'Astra process cannot be matched to its local listener; no process was stopped.' }
    }
    if (-not (Test-Path -LiteralPath $leasePath)) { throw 'Astra ownership lease is missing.' }
    return $state
}
$child = $null
$stoppedNonce = $null
if ((Test-Path -LiteralPath $leasePath) -and (Test-Path -LiteralPath $statePath)) {
    $boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime()
    if ((Get-Item -LiteralPath $statePath).LastWriteTimeUtc -lt $boot -and (Get-Item -LiteralPath $leasePath).LastWriteTimeUtc -lt $boot) {
        $prior = Read-AstraReceipt $statePath
        if ($prior.owner_id -ne $OwnerId -or (Resolve-AstraPath $prior.workspace) -ne $expectedWorkspace -or (Resolve-AstraPath $prior.python) -ne $python -or $prior.nonce -notmatch '^[a-f0-9]{64}$' -or $prior.status -notin @('starting', 'running', 'stopped')) { throw 'Previous-boot receipt has a different owner or workspace.' }
        $reusedPid = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + [int]$prior.pid)
        if ($reusedPid) {
            $matchingOwner = $reusedPid.ExecutablePath -in @($python, $basePython) -and $reusedPid.CommandLine -match [regex]::Escape((Join-Path $astraRoot 'launch_astra.py')) -and $reusedPid.CommandLine -match ('--owner-id"?\s+"?' + [regex]::Escape($OwnerId) + '(?:"|\s|$)')
            if (-not $reusedPid.CreationDate -or $reusedPid.CreationDate.ToUniversalTime() -lt $boot -or $matchingOwner) { throw 'Previous PID cannot safely be distinguished from a current Astra owner; no state was moved.' }
        }
        if ($CheckOnly) { throw 'Previous-boot receipt requires an ordinary app launch.' }
        $archive = Resolve-AstraPath (Join-Path $owner ('prior-boot-' + [guid]::NewGuid().ToString('N')))
        $null = New-Item -ItemType Directory -Path $archive
        foreach ($record in @($statePath, $accessPath, $leasePath)) {
            if (Test-Path -LiteralPath $record) {
                $safeRecord = Resolve-AstraPath $record
                Move-Item -LiteralPath $safeRecord -Destination (Join-Path $archive ([IO.Path]::GetFileName($safeRecord)))
            }
        }
    }
}
if (Test-Path -LiteralPath $leasePath) {
    if (-not (Test-Path -LiteralPath $statePath)) { throw 'Astra lease has no owner receipt; no state was removed.' }
    $null = Read-OwnedState
} else {
    if ($CheckOnly) { throw 'Astra is not running.' }
    if (Test-Path -LiteralPath $statePath) {
        $old = Read-AstraReceipt $statePath
        if ($old.status -ne 'stopped' -or $old.owner_id -ne $OwnerId -or (Resolve-AstraPath $old.workspace) -ne $expectedWorkspace) { throw 'Retained Astra owner state does not permit a new launch.' }
        $stoppedNonce = $old.nonce
    }
    $null = New-Item -ItemType Directory -Path $owner -Force
    $quotedArguments = @($arguments | ForEach-Object { '"' + $_.Replace('"', '\"') + '"' })
    $child = Start-Process -FilePath $python -ArgumentList $quotedArguments -WorkingDirectory $astraRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $owner 'launcher.stdout.log') -RedirectStandardError (Join-Path $owner 'launcher.stderr.log')
}
$deadline = [DateTime]::UtcNow.AddSeconds(30)
while ([DateTime]::UtcNow -lt $deadline) {
    if ($child -and $child.HasExited) { throw 'Astra exited before readiness; inspect its local launcher log.' }
    if ((Test-Path -LiteralPath $statePath) -and (Test-Path -LiteralPath $accessPath)) {
        $pending = Read-AstraReceipt $statePath
        if ($child -and $pending.status -eq 'stopped' -and $pending.nonce -eq $stoppedNonce) { Start-Sleep -Milliseconds 100; continue }
        $state = Read-OwnedState
        if ($state.status -eq 'running') {
            $access = Read-AstraReceipt $accessPath
            $url = [uri]$access.url
            if ($url.Scheme -ne 'http' -or $url.Host -ne '127.0.0.1' -or $url.Port -ne $state.port -or $url.AbsolutePath -ne '/' -or $url.UserInfo -or $url.Fragment -or $url.Query -notmatch '^\?token=[A-Za-z0-9_-]+$') { throw 'Invalid local Astra access receipt.' }
            $healthy = $false
            try {
                $health = Invoke-WebRequest -UseBasicParsing -Uri ('http://127.0.0.1:' + $state.port + '/api/health') -Headers @{ 'X-Session-Token' = $url.Query.Substring(7) } -TimeoutSec 2
                $body = $health.Content | ConvertFrom-Json
                $healthy = $health.StatusCode -eq 200 -and $body.ok -eq $true -and $body.service -eq 'question-json-studio'
            } catch { $healthy = $false }
            if ($healthy) {
                $current = Read-OwnedState
                if ($current.nonce -ne $state.nonce -or $current.pid -ne $state.pid) { throw 'Astra owner changed during readiness.' }
                if (-not $CheckOnly) { Start-Process -FilePath $url.AbsoluteUri }
                Write-Output ('Astra ready on local port ' + $state.port)
                exit 0
            }
        }
    }
    Start-Sleep -Milliseconds 100
}
throw 'Astra readiness timed out; no state was removed or process killed.'
