#requires -Version 5.1
<#
.SYNOPSIS
    Run the Golden Pagoda Discord bot locally on Windows (PowerShell).

.DESCRIPTION
    Creates/uses a local virtual environment, installs dependencies, verifies
    a .env exists, optionally checks the local Ollama server, then launches the
    bot with `python bot.py`. Logs stream to this terminal; Ctrl+C stops it.

.EXAMPLE
    ./run.ps1
        Install deps (first run) and start the bot.

.EXAMPLE
    ./run.ps1 -NoInstall
        Skip the dependency install step and start the bot immediately.
#>
param(
    [switch]$NoInstall
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

# --- locate Python ------------------------------------------------------------
$python = $null
foreach ($candidate in @('python', 'py')) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) { $python = $candidate; break }
}
if (-not $python) {
    Write-Error "Python not found on PATH. Install Python 3.12+ from https://python.org and re-run."
    exit 1
}

# --- virtual environment ------------------------------------------------------
$venv = Join-Path $PSScriptRoot '.venv'
$venvPython = Join-Path $venv 'Scripts/python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Host '>> creating virtual environment (.venv)' -ForegroundColor Cyan
    & $python -m venv $venv
}

# --- dependencies -------------------------------------------------------------
if (-not $NoInstall) {
    Write-Host '>> installing dependencies' -ForegroundColor Cyan
    & $venvPython -m pip install --quiet --upgrade pip
    & $venvPython -m pip install --quiet -r (Join-Path $PSScriptRoot 'requirements.txt')
}

# --- .env check ---------------------------------------------------------------
$envFile = Join-Path $PSScriptRoot '.env'
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $PSScriptRoot '.env.example') $envFile
    Write-Warning "Created .env from template. Fill in DISCORD_TOKEN + TARGET_CHANNEL_ID, then re-run."
    exit 1
}

# --- data dir -----------------------------------------------------------------
$dataDir = Join-Path $PSScriptRoot 'data'
if (-not (Test-Path $dataDir)) { New-Item -ItemType Directory -Path $dataDir | Out-Null }

# --- optional Ollama health check --------------------------------------------
$ollamaModel = (Select-String -Path $envFile -Pattern '^OLLAMA_OCR_MODEL=(.+)$' -ErrorAction SilentlyContinue).Matches.Groups[1].Value
if ($ollamaModel) {
    $ollamaUrl = (Select-String -Path $envFile -Pattern '^OLLAMA_URL=(.+)$' -ErrorAction SilentlyContinue).Matches.Groups[1].Value
    if (-not $ollamaUrl) { $ollamaUrl = 'http://localhost:11434' }
    try {
        Invoke-RestMethod -Uri "$ollamaUrl/api/tags" -TimeoutSec 3 | Out-Null
        Write-Host ">> Ollama reachable at $ollamaUrl (model: $ollamaModel)" -ForegroundColor Green
    } catch {
        Write-Warning "OLLAMA_OCR_MODEL is set but Ollama isn't reachable at $ollamaUrl. Start it with 'ollama serve' (OCR will fall back to OCR.space/Tesseract)."
    }
}

# --- run ----------------------------------------------------------------------
Write-Host '>> starting Golden Pagoda bot (Ctrl+C to stop)' -ForegroundColor Cyan
& $venvPython (Join-Path $PSScriptRoot 'bot.py')
