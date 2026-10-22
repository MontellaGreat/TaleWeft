param([int]$Port = 8018, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$PreviewRoot = Join-Path $ProjectRoot 'out\author-desk-preview'
$PythonRuntime = Join-Path (Split-Path $ProjectRoot -Parent) '.ws8-verify-venv\Scripts\python.exe'
$PrivateLinkPath = Join-Path $PreviewRoot 'session-private.url'
$StatePath = Join-Path $PreviewRoot 'runtime-state.json'
function Open-OwnedPreview {
    if (!(Test-Path -LiteralPath $StatePath)) { return $false }
    $PreviewState = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    if ($PreviewState.data -ne (Join-Path $PreviewRoot 'data')) { return $false }
    $OwnerProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($PreviewState.launcher_pid)" -ErrorAction SilentlyContinue
    if (!$OwnerProcess -or !$OwnerProcess.CommandLine -or $OwnerProcess.CommandLine.IndexOf($PSCommandPath, [StringComparison]::OrdinalIgnoreCase) -lt 0) { return $false }
    $ExistingListener = Get-NetTCPConnection -LocalPort $PreviewState.port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (!$ExistingListener) { return $false }
    $CursorProcessId = $ExistingListener.OwningProcess
    $OwnedListener = $false
    for ($AncestorDepth = 0; $AncestorDepth -lt 12 -and $CursorProcessId; $AncestorDepth++) {
        if ($CursorProcessId -eq $PreviewState.launcher_pid) { $OwnedListener = $true; break }
        $AncestorProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $CursorProcessId" -ErrorAction SilentlyContinue
        if (!$AncestorProcess) { break }
        $CursorProcessId = $AncestorProcess.ParentProcessId
    }
    if (!$OwnedListener) { return $false }
    $PrivateSource = if (Test-Path -LiteralPath $PrivateLinkPath) { Get-Content -LiteralPath $PrivateLinkPath -Raw } else { Get-Content -LiteralPath (Join-Path $PreviewRoot 'launch-private.log') -Raw }
    $LinkMatch = [regex]::Match($PrivateSource, 'http://127\.0\.0\.1:' + $PreviewState.port + '/#plotpilot-session=[A-Za-z0-9_-]+')
    if (!$LinkMatch.Success) { throw 'The preview is running, but its session link is not ready. Retry shortly.' }
    if (!$NoBrowser) { Start-Process -FilePath $LinkMatch.Value }
    Write-Host "Existing TaleWeft preview reopened at http://127.0.0.1:$($PreviewState.port) . No second backend was started."
    return $true
}
if (!(Test-Path -LiteralPath $PythonRuntime)) { throw 'Missing local Python runtime: .ws8-verify-venv' }
if (!(Test-Path -LiteralPath (Join-Path $ProjectRoot 'frontend\dist\index.html'))) { throw 'Build frontend first: npm run build in frontend' }
New-Item -ItemType Directory -Force -Path (Join-Path $PreviewRoot 'data') | Out-Null
$env:TALEWEFT_PROD_DATA_DIR = Join-Path $PreviewRoot 'data'
$env:PLOTPILOT_PROD_DATA_DIR = $env:TALEWEFT_PROD_DATA_DIR
$env:PYTHONIOENCODING = 'utf-8'
Set-Location -LiteralPath $ProjectRoot
$DataIdentity = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($env:TALEWEFT_PROD_DATA_DIR.ToLowerInvariant())).Replace('/', '_')
$DataMutex = New-Object System.Threading.Mutex($false, ('Local\TaleWeft.AuthorDesk.' + $DataIdentity))
$HasDataMutex = $false
try {
try { $HasDataMutex = $DataMutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $HasDataMutex = $true }
if (Open-OwnedPreview) { exit 0 }
if (!$HasDataMutex) { throw 'This preview data directory is already starting or running. Retry its launcher shortly.' }
$ActiveListener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($ActiveListener) { throw "Port $Port is already occupied. Use the existing browser session or stop its launcher first." }
@{ launcher_pid = $PID; port = $Port; data = $env:TALEWEFT_PROD_DATA_DIR; started = (Get-Date).ToString('o') } | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $PreviewRoot 'runtime-state.json')
$ErrorActionPreference = 'Continue'
& $PythonRuntime scripts/verification/author_desk_seed.py *> (Join-Path $PreviewRoot 'seed.log')
if ($LASTEXITCODE -ne 0) { throw 'Sample initialization failed. Inspect out/author-desk-preview/seed.log' }
Write-Host "Opening isolated TaleWeft at http://127.0.0.1:$Port . Keep this window open; Ctrl+C stops the service."
if (Test-Path -LiteralPath $PrivateLinkPath) { Remove-Item -LiteralPath $PrivateLinkPath }
$LaunchArgs = @('scripts/run_local_secure.py', '--port', "$Port", '--private-link-file', $PrivateLinkPath)
if ($NoBrowser) { $LaunchArgs += '--no-browser' }
& $PythonRuntime @LaunchArgs *> (Join-Path $PreviewRoot 'launch-private.log')
exit $LASTEXITCODE
} finally {
    if ($HasDataMutex) { $DataMutex.ReleaseMutex() }
    $DataMutex.Dispose()
}
