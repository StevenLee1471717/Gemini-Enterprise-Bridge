param(
    [switch]$Test,
    [switch]$ExportOnly,
    [switch]$SkipUpdate,
    [switch]$Stop,
    [switch]$Status,
    [switch]$Background,
    [switch]$EnsureCodexProfile,
    [switch]$InstallStartup,
    [switch]$UninstallStartup,
    [switch]$Force,
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
Set-Location -Path $PSScriptRoot

function Resolve-HostPython {
    $candidates = @(
        @("py", "-3"),
        @("python"),
        @("python3")
    )
    $extraDirs = @()
    if ($env:LOCALAPPDATA) {
        $extraDirs += Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA "Programs\Python\Python3*") -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending
    }
    if ($env:ProgramFiles) {
        $extraDirs += Get-ChildItem -Path (Join-Path $env:ProgramFiles "Python3*") -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending
    }
    foreach ($dir in $extraDirs) {
        $exe = Join-Path $dir.FullName "python.exe"
        if (Test-Path $exe) {
            $candidates += , @($exe)
        }
    }

    foreach ($cand in $candidates) {
        $bin = $cand[0]
        $extraArgs = if ($cand.Count -gt 1) { $cand[1..($cand.Count - 1)] } else { @() }
        try {
            $cmdObj = Get-Command $bin -ErrorAction SilentlyContinue
            if (-not $cmdObj -and -not (Test-Path $bin)) {
                continue
            }
            $probe = & $bin @extraArgs -c "import sys; assert sys.version_info >= (3, 10); print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $probe) {
                $resolvedExe = ($probe | Select-Object -Last 1).Trim()
                if ($resolvedExe -and (Test-Path $resolvedExe) -and ($resolvedExe -notmatch "WindowsApps\\python")) {
                    return $resolvedExe
                }
            }
        } catch {
            continue
        }
    }
    throw "Python 3.10+ was not found (or only Microsoft Store stub exists). Install Python via: winget install -e --id Python.Python.3.12"
}

function Resolve-ActivePort {
    param([int]$OverridePort)
    if ($OverridePort -gt 0) {
        return $OverridePort
    }
    $portVal = 8000
    if ($env:BRIDGE_PORT) {
        [void][int]::TryParse($env:BRIDGE_PORT, [ref]$portVal)
    } elseif (Test-Path ".env") {
        $portLine = Get-Content ".env" -Encoding UTF8 | Where-Object { $_ -match "^\s*BRIDGE_PORT\s*=\s*(\d+)" } | Select-Object -First 1
        if ($portLine -match "(\d+)") {
            $portVal = [int]$Matches[1]
        }
    }
    return $portVal
}

function Get-PortListenerPid {
    param([int]$TargetPort)
    try {
        $conn = Get-NetTCPConnection -LocalPort $TargetPort -State Listen -ErrorAction Stop | Select-Object -First 1
        if ($conn -and $conn.OwningProcess -gt 0) {
            return [int]$conn.OwningProcess
        }
    } catch {
        $lines = netstat -ano 2>$null | Select-String -Pattern "LISTENING"
        foreach ($ln in $lines) {
            $text = $ln.Line.Trim()
            if ($text -match ":$TargetPort\s+\S+\s+LISTENING\s+(\d+)$") {
                return [int]$Matches[1]
            }
        }
    }
    return 0
}

function Get-BridgeLiveStatus {
    param([int]$TargetPort)
    try {
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:$TargetPort/api/status" -Method Get -TimeoutSec 2 -ErrorAction Stop
        return $resp
    } catch {
        return $null
    }
}

function Stop-BridgeInstance {
    param([int]$TargetPort, [switch]$ForceStop)
    $live = Get-BridgeLiveStatus -TargetPort $TargetPort
    if ($live -and $live.hub -and (-not $ForceStop)) {
        $inFlight = [int]($live.hub.in_flight_requests)
        $queued = [int]($live.hub.queued_requests)
        if (($inFlight -gt 0) -or ($queued -gt 0)) {
            Write-Host "[Busy] Port $TargetPort currently has in_flight=$inFlight, queued=$queued requests. Pass -Force to stop anyway." -ForegroundColor Yellow
            exit 2
        }
    }
    $oldPid = Get-PortListenerPid -TargetPort $TargetPort
    if ($oldPid -le 0) {
        Write-Host "[Stop] No listener found on port $TargetPort." -ForegroundColor Gray
        return
    }
    Write-Host "[Stop] Stopping listener on port $TargetPort (PID=$oldPid) ..." -ForegroundColor Yellow
    try {
        Stop-Process -Id $oldPid -Force -ErrorAction Stop
        Start-Sleep -Milliseconds 700
        Write-Host "[Stop] Stopped PID=$oldPid." -ForegroundColor Green
    } catch {
        Write-Host "[Warn] Could not stop PID=$oldPid ($($_.Exception.Message))." -ForegroundColor Red
        exit 1
    }
}

$StartupVbs = Join-Path ([Environment]::GetFolderPath("Startup")) "Gemini-Enterprise-Bridge.vbs"
if ($UninstallStartup) {
    if (Test-Path $StartupVbs) {
        Remove-Item -Path $StartupVbs -Force
        Write-Host "[Startup] Removed auto-start entry: $StartupVbs" -ForegroundColor Green
    } else {
        Write-Host "[Startup] No auto-start entry found at: $StartupVbs" -ForegroundColor Gray
    }
    exit 0
}

$ActivePort = Resolve-ActivePort -OverridePort $Port
if ($Port -gt 0) {
    $env:BRIDGE_PORT = "$Port"
}

if ($Status) {
    $live = Get-BridgeLiveStatus -TargetPort $ActivePort
    if ($live -and $live.hub) {
        Write-Host "[OK] Running on http://127.0.0.1:$ActivePort/ | browser_connected=$($live.hub.browser_connected) | in_flight=$($live.hub.in_flight_requests) | queued=$($live.hub.queued_requests)" -ForegroundColor Green
        exit 0
    }
    Write-Host "[Offline] No responsive Bridge service on http://127.0.0.1:$ActivePort/" -ForegroundColor Yellow
    exit 1
}

if ($Stop) {
    Stop-BridgeInstance -TargetPort $ActivePort -ForceStop:$Force
    exit 0
}

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
    $HostPython = Resolve-HostPython
    Write-Host "[Setup] Creating isolated Python venv at: $VenvDir ..." -ForegroundColor Yellow
    $ParentDir = Split-Path -Parent $VenvDir
    if (-not (Test-Path $ParentDir)) {
        New-Item -ItemType Directory -Force -Path $ParentDir | Out-Null
    }
    & $HostPython -m venv $VenvDir
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

if ($EnsureCodexProfile) {
    $BackupDir = Join-Path $PSScriptRoot ".cache\codex-profile-backups\$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    & $VenvPython configure_codex_context.py --ensure-bridge-profile --port $ActivePort --apply --backup-dir $BackupDir
    exit $LASTEXITCODE
}

if ($InstallStartup) {
    $EscapedPs1 = $PSCommandPath.Replace('"', '""')
    $VbsLines = @(
        'Set sh = CreateObject("WScript.Shell")',
        "sh.Run `"powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"`"$EscapedPs1`"`" -Background`", 0, False"
    )
    Set-Content -Path $StartupVbs -Value ($VbsLines -join "`r`n") -Encoding ASCII
    Write-Host "[Startup] Installed Windows login auto-start: $StartupVbs" -ForegroundColor Green
    if (-not $Background) {
        exit 0
    }
}

if ($ExportOnly) {
    & $VenvPython extension_exporter.py
    exit 0
}

if ($Test) {
    & $VenvPython extension_exporter.py
    Write-Host "[Test] Running automated test suite (pytest) ..." -ForegroundColor Cyan
    & $VenvPython -m pytest -v
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    $NodeCmd = Get-Command node -ErrorAction SilentlyContinue
    if ($NodeCmd) {
        Write-Host "[Test] Running Node.js extension & Shadow DOM suites ..." -ForegroundColor Cyan
        Get-ChildItem -Path $PSScriptRoot -Filter "test_*.mjs" | Sort-Object Name | ForEach-Object {
            Write-Host "  -> $($_.Name)" -ForegroundColor Gray
            & $NodeCmd.Source $_.FullName
            if ($LASTEXITCODE -ne 0) {
                exit $LASTEXITCODE
            }
        }
    }
    exit 0
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

if (Test-Path "sync_client_limits.py") {
    try {
        $SyncJson = & $VenvPython sync_client_limits.py
        $SyncObj = $SyncJson | ConvertFrom-Json -ErrorAction SilentlyContinue
        if ($SyncObj -and $SyncObj.models_updated -and $SyncObj.models_updated.Count -gt 0) {
            Write-Host "[Limits] Synced 1M context / 65K output token limits for $($SyncObj.models_updated.Count) Copilot BYOK model(s)." -ForegroundColor Green
        }
    } catch {
        # Best-effort client DB sync; never block startup
    }
}

if ($Port -gt 0) {
    $env:BRIDGE_PORT = "$Port"
}

if ($Background) {
    $ExistingLive = Get-BridgeLiveStatus -TargetPort $ActivePort
    if ($ExistingLive -and $ExistingLive.hub) {
        Write-Host "[Running] Gemini Enterprise Bridge is already active on http://127.0.0.1:${ActivePort}/" -ForegroundColor Green
        exit 0
    }
    $ExistingPid = Get-PortListenerPid -TargetPort $ActivePort
    if ($ExistingPid -gt 0) {
        Stop-BridgeInstance -TargetPort $ActivePort -ForceStop:$Force
    }
    $LogsDir = Join-Path (Split-Path -Parent $PSScriptRoot) "logs"
    if (-not (Test-Path $LogsDir)) {
        New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null
    }
    $StdOutLog = Join-Path $LogsDir "server.log"
    $StdErrLog = Join-Path $LogsDir "server-error.log"
    Start-Process -FilePath $VenvPython -ArgumentList @("server.py") -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput $StdOutLog -RedirectStandardError $StdErrLog | Out-Null
    for ($i = 0; $i -lt 25; $i++) {
        Start-Sleep -Milliseconds 400
        $check = Get-BridgeLiveStatus -TargetPort $ActivePort
        if ($check -and $check.hub) {
            Write-Host "[Started] Gemini Enterprise Bridge is running in background: http://127.0.0.1:${ActivePort}/" -ForegroundColor Green
            exit 0
        }
    }
    Write-Host "[Error] Background service did not become ready within 10s. Check logs at: $StdErrLog" -ForegroundColor Red
    exit 1
}

$ExtFolder = Join-Path $PSScriptRoot "extension"
Write-Host ""
Write-Host "  1. Load unpacked extension from: $ExtFolder" -ForegroundColor Green
Write-Host "  2. Sign in at: https://vertexaisearch.cloud.google.com" -ForegroundColor Green
Write-Host "  3. OpenAI Base URL: http://127.0.0.1:${ActivePort}/v1  (Models: gemini-4-argon / gemini-4-carbon / gemini-3.1-pro / gemini-3.8-flash)" -ForegroundColor Green
Write-Host "  4. Local Dashboard: http://127.0.0.1:${ActivePort}/" -ForegroundColor Green
Write-Host ""

# Safely check in-flight requests before replacing any existing listener occupying $ActivePort
$OldPid = Get-PortListenerPid -TargetPort $ActivePort
if ($OldPid -gt 0) {
    Stop-BridgeInstance -TargetPort $ActivePort -ForceStop:$Force
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
