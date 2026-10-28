param([string]$PreviewRoot = '')

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (!$PreviewRoot) { $PreviewRoot = Join-Path $ProjectRoot 'out\taleweft-web-preview' }
$PreviewRoot = (Resolve-Path -LiteralPath $PreviewRoot).Path
$PythonRuntime = Join-Path (Split-Path $ProjectRoot -Parent) '.ws8-verify-venv\Scripts\python.exe'
$LinkFile = Join-Path $PreviewRoot 'session-private.url'
$StateFile = Join-Path $PreviewRoot 'runtime-state.json'
$Port = 8019
$Listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($Listener) {
    if (!(Test-Path -LiteralPath $StateFile) -or !(Test-Path -LiteralPath $LinkFile)) { throw 'Port 8019 belongs to another application.' }
    $State = Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json
    $Owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($State.launcher_pid)" -ErrorAction SilentlyContinue
    if (!$Owner -or !$Owner.CommandLine.Contains('scripts/run_local_secure.py')) { throw 'Cannot verify this preview owner.' }
    Start-Process -FilePath (Get-Content -LiteralPath $LinkFile -Raw)
    Write-Host 'TaleWeft Web reopened: http://127.0.0.1:8019'
    exit 0
}
$env:TALEWEFT_PROD_DATA_DIR = Join-Path $PreviewRoot 'data'
$env:PLOTPILOT_PROD_DATA_DIR = $env:TALEWEFT_PROD_DATA_DIR
$env:PYTHONIOENCODING = 'utf-8'
$env:LOG_FILE = Join-Path $PreviewRoot 'backend.log'
# Autopilot commands need the owned daemon process to acknowledge and execute.
$env:DISABLE_AUTO_DAEMON = '0'
if (Test-Path -LiteralPath $LinkFile) { Remove-Item -LiteralPath $LinkFile }
$Arguments = @('scripts/run_local_secure.py', '--port', "$Port", '--no-browser', '--private-link-file', $LinkFile)
$Process = Start-Process -FilePath $PythonRuntime -ArgumentList $Arguments -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $PreviewRoot 'launcher.log') -RedirectStandardError (Join-Path $PreviewRoot 'launcher-error.log')
@{ launcher_pid = $Process.Id; port = $Port; data = $env:TALEWEFT_PROD_DATA_DIR; started = (Get-Date).ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath $StateFile -Encoding UTF8
$Deadline = (Get-Date).AddSeconds(100)
while ((Get-Date) -lt $Deadline) {
    if ($Process.HasExited) { throw 'Web backend exited. Inspect launcher-error.log.' }
    if (Test-Path -LiteralPath $LinkFile) {
        Start-Process -FilePath (Get-Content -LiteralPath $LinkFile -Raw)
        Write-Host 'TaleWeft Web ready: http://127.0.0.1:8019'
        exit 0
    }
    Start-Sleep -Milliseconds 500
}
throw 'Web backend is still starting. Inspect launcher-error.log.'
