#requires -Version 5.1
<#
.SYNOPSIS
    Install the Golden Pagoda Discord bot as a background Windows service (NSSM).

.DESCRIPTION
    Runs the bot via `.venv\Scripts\python.exe bot.py` as a Windows service so it
    starts on boot, restarts on crash, and runs without a logged-in terminal.

    The script:
      - requires an elevated (Administrator) PowerShell;
      - creates a local `.venv` and installs requirements if missing;
      - copies `.env.example` to `.env` on first run;
      - records the bot directory in the `GP_BOT_DIR` machine environment
        variable so the GitHub Actions deploy workflow knows where to pull;
      - installs/updates an NSSM service ("GoldenPagoda") with auto-start,
        crash-restart, and rotating stdout/stderr logs under `data\`.

    Install NSSM first:  winget install NSSM.NSSM

.PARAMETER BotDir
    Path to the cloned repo. Defaults to the parent folder of this script.

.PARAMETER ServiceName
    Windows service name. Default: GoldenPagoda.

.EXAMPLE
    ./scripts/install-service.ps1
        Install/refresh the service from the repo this script lives in.

.EXAMPLE
    ./scripts/install-service.ps1 -BotDir C:\GoldenPagoda
        Install the service for a repo cloned at C:\GoldenPagoda.
#>
param(
    [string]$BotDir = (Split-Path -Parent $PSScriptRoot),
    [string]$ServiceName = 'GoldenPagoda'
)

$ErrorActionPreference = 'Stop'

# --- require admin ------------------------------------------------------------
$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    Write-Error "Run this in an elevated PowerShell (Run as Administrator)."
    exit 1
}

# --- locate NSSM --------------------------------------------------------------
$nssm = (Get-Command nssm -ErrorAction SilentlyContinue).Source
if (-not $nssm) {
    Write-Error "NSSM not found on PATH. Install it ('winget install NSSM.NSSM' or 'choco install nssm'), then re-run."
    exit 1
}

# --- resolve paths ------------------------------------------------------------
$BotDir = (Resolve-Path $BotDir).Path
$venvPython = Join-Path $BotDir '.venv\Scripts\python.exe'
$botScript = Join-Path $BotDir 'bot.py'
$dataDir = Join-Path $BotDir 'data'

if (-not (Test-Path $botScript)) {
    Write-Error "bot.py not found in '$BotDir'. Pass -BotDir <path to the cloned repo>."
    exit 1
}

# --- locate Python for venv creation -----------------------------------------
$python = $null
foreach ($candidate in @('python', 'py')) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) { $python = $candidate; break }
}
if (-not $python) {
    Write-Error "Python not found on PATH. Install Python 3.12+ (for all users) and re-run."
    exit 1
}

# --- venv + dependencies ------------------------------------------------------
if (-not (Test-Path $venvPython)) {
    Write-Host '>> creating virtual environment (.venv)' -ForegroundColor Cyan
    & $python -m venv (Join-Path $BotDir '.venv')
}
Write-Host '>> installing dependencies' -ForegroundColor Cyan
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r (Join-Path $BotDir 'requirements.txt')

# --- data dir + .env ----------------------------------------------------------
if (-not (Test-Path $dataDir)) { New-Item -ItemType Directory -Path $dataDir | Out-Null }
$envFile = Join-Path $BotDir '.env'
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $BotDir '.env.example') $envFile
    Write-Warning "Created .env at $envFile. Fill in DISCORD_TOKEN + TARGET_CHANNEL_ID before the service can connect."
}

# --- record bot dir for the deploy workflow -----------------------------------
[Environment]::SetEnvironmentVariable('GP_BOT_DIR', $BotDir, 'Machine')
$env:GP_BOT_DIR = $BotDir
Write-Host ">> set machine env var GP_BOT_DIR=$BotDir" -ForegroundColor Green

# --- install / update the service --------------------------------------------
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host ">> updating existing service '$ServiceName'" -ForegroundColor Cyan
    if ($existing.Status -eq 'Running') { & $nssm stop $ServiceName | Out-Null }
    & $nssm set $ServiceName Application $venvPython
    & $nssm set $ServiceName AppParameters $botScript
    & $nssm set $ServiceName AppDirectory $BotDir
} else {
    Write-Host ">> installing service '$ServiceName'" -ForegroundColor Cyan
    & $nssm install $ServiceName $venvPython $botScript
    & $nssm set $ServiceName AppDirectory $BotDir
}

# --- service metadata, logging + restart policy ------------------------------
& $nssm set $ServiceName DisplayName 'Golden Pagoda Discord Bot'
& $nssm set $ServiceName Description 'OCR Warframe profile verification bot.'
& $nssm set $ServiceName Start SERVICE_AUTO_START
& $nssm set $ServiceName AppStdout (Join-Path $dataDir 'service.out.log')
& $nssm set $ServiceName AppStderr (Join-Path $dataDir 'service.err.log')
& $nssm set $ServiceName AppRotateFiles 1
& $nssm set $ServiceName AppRotateBytes 10485760
& $nssm set $ServiceName AppExit Default Restart
& $nssm set $ServiceName AppRestartDelay 5000

# --- start --------------------------------------------------------------------
& $nssm start $ServiceName
Write-Host ">> '$ServiceName' started. Logs: $dataDir\service.out.log / service.err.log" -ForegroundColor Green
Write-Host ">> manage with: nssm restart $ServiceName | Stop-Service $ServiceName | Get-Service $ServiceName"
