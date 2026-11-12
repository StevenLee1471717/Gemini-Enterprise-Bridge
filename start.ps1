param(
    [switch]$Test,
    [switch]$ExportOnly,
    [switch]$SkipUpdate,
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location -Path $PSScriptRoot

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Gemini Enterprise Web-to-API Bridge (Local OpenAI Proxy)  " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# Initialize .env and .gitignore on first launch if absent
if ((-not (Test-Path ".env")) -and (Test-Path "env.example")) {
    Copy-Item "env.example" ".env"
    Write-Host "[Init] Created default .env from env.example" -ForegroundColor Green
}

if (-not (Test-Path ".gitignore")) {
    @(
        "__pycache__/",
        "*.py[cod]",
        ".venv/",
        "venv/",
        ".pytest_cache/",
        ".env",
        "*.log"
    ) | Set-Content -Path ".gitignore" -Encoding UTF8
}

# Keep virtual environment outside root by default so Chrome unpacked extension loading stays instant
$LocalVenv = Join-Path $PSScriptRoot ".venv"
$AppDataVenv = Join-Path $env:LOCALAPPDATA "Gemini-Enterprise-Bridge\venv"
$VenvDir = if (Test-Path (Join-Path $LocalVenv "Scripts\python.exe")) { $LocalVenv } else { $AppDataVenv }
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "[Setup] Creating isolated Python venv at: $VenvDir ..." -ForegroundColor Yellow
    $ParentDir = Split-Path -Parent $VenvDir
    if (-not (Test-Path $ParentDir)) {
        New-Item -ItemType Directory -Force -Path $ParentDir | Out-Null
    }
    python -m venv $VenvDir
}

$ReqHashFile = Join-Path $VenvDir ".req.sha256"
$CurrentHash = (Get-FileHash -Path "requirements.txt" -Algorithm SHA256).Hash
$InstalledHash = if (Test-Path $ReqHashFile) { (Get-Content $ReqHashFile -Raw).Trim() } else { "" }

if ($CurrentHash -ne $InstalledHash) {
    Write-Host "[Setup] Installing/updating Python dependencies (requirements.txt) ..." -ForegroundColor Yellow
    & $VenvPython -m pip install --upgrade pip --quiet
    & $VenvPython -m pip install -r requirements.txt
    Set-Content -Path $ReqHashFile -Value $CurrentHash -NoNewline
}

function Invoke-SyncDependenciesAndExtension {
    $LatestReqHash = (Get-FileHash -Path "requirements.txt" -Algorithm SHA256).Hash
    $SavedReqHash = if (Test-Path $ReqHashFile) { (Get-Content $ReqHashFile -Raw).Trim() } else { "" }
    if ($LatestReqHash -ne $SavedReqHash) {
        Write-Host "[Setup] Updating Python dependencies after release sync ..." -ForegroundColor Yellow
        & $VenvPython -m pip install -r requirements.txt
        Set-Content -Path $ReqHashFile -Value $LatestReqHash -NoNewline
    }
    & $VenvPython extension_exporter.py
}

if ($ExportOnly) {
    & $VenvPython extension_exporter.py
    exit 0
}

if ($Test) {
    & $VenvPython extension_exporter.py
    Write-Host "[Test] Running automated test suite (pytest) ..." -ForegroundColor Cyan
    & $VenvPython -m pytest -v
    exit $LASTEXITCODE
}

# Check GitHub Releases for backend & extension updates on every startup unless disabled
$AutoUpdateDisabled = ($SkipUpdate -or ($env:BRIDGE_AUTO_UPDATE -match "^(0|false|no|off)$"))
if (-not $AutoUpdateDisabled -and (Test-Path "updater.py")) {
    try {
        & $VenvPython updater.py --startup-update
    } catch {
        Write-Host "[Updater] Startup update check skipped: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

# Always materialize clean `extension\` (with generated PNG icons) and `userscript\`
Invoke-SyncDependenciesAndExtension

if ($Port -gt 0) {
    $env:BRIDGE_PORT = "$Port"
}

$ActivePort = 8000
if ($env:BRIDGE_PORT) {
    [void][int]::TryParse($env:BRIDGE_PORT, [ref]$ActivePort)
} elseif (Test-Path ".env") {
    $portLine = Get-Content ".env" | Where-Object { $_ -match "^\s*BRIDGE_PORT\s*=\s*(\d+)" } | Select-Object -First 1
    if ($portLine -match "(\d+)") {
        $ActivePort = [int]$Matches[1]
    }
}

$ExtFolder = Join-Path $PSScriptRoot "extension"
Write-Host ""
Write-Host "  1. Load unpacked extension from: $ExtFolder" -ForegroundColor Green
Write-Host "  2. Sign in at: https://vertexaisearch.cloud.google.com" -ForegroundColor Green
Write-Host "  3. OpenAI Base URL: http://127.0.0.1:${ActivePort}/v1  (Models: gemini-4-argon / gemini-4-carbon / gemini-3.1-pro / gemini-3.8-flash)" -ForegroundColor Green
Write-Host "  4. Local Dashboard: http://127.0.0.1:${ActivePort}/" -ForegroundColor Green
Write-Host ""

# Gracefully replace any stale server.py listener occupying $ActivePort
$ExistingListener = Get-NetTCPConnection -LocalPort $ActivePort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($ExistingListener) {
    $OldPid = $ExistingListener.OwningProcess
    Write-Host "[Port] Port ${ActivePort} is currently in use by PID=${OldPid}; stopping old instance..." -ForegroundColor Yellow
    try {
        Stop-Process -Id $OldPid -Force -ErrorAction Stop
        Start-Sleep -Milliseconds 800
        Write-Host "[Port] Stopped previous instance (PID=${OldPid})." -ForegroundColor Green
    } catch {
        Write-Host "[Warn] Could not stop PID=${OldPid} ($($_.Exception.Message)). Close the old terminal if bind fails." -ForegroundColor Red
    }
}

$env:BRIDGE_SUPERVISED = "1"
$MaxRestarts = 3
$RestartCount = 0
while ($true) {
    Write-Host "[Server] Running on http://127.0.0.1:${ActivePort} (WebSocket: ws://127.0.0.1:${ActivePort}/ws/browser)" -ForegroundColor Cyan
    & $VenvPython server.py
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -eq 42) {
        Write-Host "[Updater] Hot-restart requested (code=42); applying update and restarting server..." -ForegroundColor Cyan
        if (Test-Path "updater.py") {
            try {
                & $VenvPython updater.py --startup-update
            } catch {
                Write-Host "[Updater] Post-exit update check warning: $($_.Exception.Message)" -ForegroundColor Yellow
            }
        }
        Invoke-SyncDependenciesAndExtension
        $RestartCount = 0
        Start-Sleep -Milliseconds 600
        continue
    }
    if ($ExitCode -eq 0 -or $RestartCount -ge $MaxRestarts) {
        if ($ExitCode -ne 0) {
            Write-Host "[Error] server.py exited with code ${ExitCode} after ${MaxRestarts} recovery attempts." -ForegroundColor Red
            exit $ExitCode
        }
        break
    }
    $RestartCount = $RestartCount + 1
    Write-Host "[Recovery] server.py exited unexpectedly (code=${ExitCode}); restarting in 2s (${RestartCount}/${MaxRestarts})..." -ForegroundColor Yellow
    Start-Sleep -Seconds 2
}
