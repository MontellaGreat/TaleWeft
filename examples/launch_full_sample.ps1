param(
    [string]$Executable,
    [string]$SampleArchive,
    [string]$DataDirectory,
    [switch]$UseExisting,
    [switch]$PrepareOnly
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms

function Select-File([string]$Title, [string]$Filter, [string]$InitialDirectory) {
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = $Title
    $dialog.Filter = $Filter
    $dialog.CheckFileExists = $true
    if (Test-Path -LiteralPath $InitialDirectory) { $dialog.InitialDirectory = $InitialDirectory }
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        throw 'Operation cancelled.'
    }
    return $dialog.FileName
}

if (-not $Executable) {
    $Executable = Select-File 'Select installed TaleWeft.exe' 'TaleWeft.exe|TaleWeft.exe|Applications (*.exe)|*.exe' $env:LOCALAPPDATA
}
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf) -or
    [IO.Path]::GetFileName($Executable) -ne 'TaleWeft.exe') {
    throw 'Select TaleWeft.exe from the installed application directory.'
}

if (-not $SampleArchive) {
    $SampleArchive = Select-File 'Select the TaleWeft full sample archive' 'ZIP files (*.zip)|*.zip' (Split-Path -Parent $PSCommandPath)
}
if (-not (Test-Path -LiteralPath $SampleArchive -PathType Leaf) -or
    [IO.Path]::GetExtension($SampleArchive) -ne '.zip') {
    throw 'The sample data archive does not exist or is not a ZIP file.'
}

if (-not $DataDirectory) {
    $DataDirectory = Join-Path $env:LOCALAPPDATA ('TaleWeft-Sample-' + [guid]::NewGuid().ToString('N'))
}
$DataDirectory = [IO.Path]::GetFullPath($DataDirectory)
if ($UseExisting) {
    if (-not (Test-Path -LiteralPath (Join-Path $DataDirectory 'plotpilot.db') -PathType Leaf)) {
        throw "Existing sample directory has no plotpilot.db: $DataDirectory"
    }
} else {
    if (Test-Path -LiteralPath $DataDirectory) {
        if ((Get-ChildItem -LiteralPath $DataDirectory -Force | Measure-Object).Count -ne 0) {
            throw "The destination is non-empty; stopping to protect existing data: $DataDirectory"
        }
    } else {
        New-Item -ItemType Directory -Path $DataDirectory | Out-Null
    }
    Expand-Archive -LiteralPath $SampleArchive -DestinationPath $DataDirectory -Force
}
$database = Join-Path $DataDirectory 'plotpilot.db'
if (-not (Test-Path -LiteralPath $database -PathType Leaf)) {
    throw 'The sample data archive does not contain plotpilot.db.'
}

Write-Host "Full sample database extracted to an isolated directory: $DataDirectory"
Write-Host 'This directory is isolated from the default library. Reuse it with -UseExisting -DataDirectory <directory>.'
if (-not $PrepareOnly) {
    $env:TALEWEFT_PROD_DATA_DIR = $DataDirectory
    $env:PLOTPILOT_PROD_DATA_DIR = $DataDirectory
    $env:AITEXT_PROD_DATA_DIR = $DataDirectory
    Start-Process -FilePath $Executable
}
