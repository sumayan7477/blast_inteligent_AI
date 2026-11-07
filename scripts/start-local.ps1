<#
.SYNOPSIS
  Start Ollama, the backend and the frontend LOCALLY (127.0.0.1 only), skipping anything already running.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\start-local.ps1
  powershell -ExecutionPolicy Bypass -File scripts\start-local.ps1 -Model llama3.1:8b
#>
param(
    [string]$Model = $(if ($env:BLAST_OLLAMA_MODEL) { $env:BLAST_OLLAMA_MODEL } else { "deepseek-r1:1.5b" }),
    [switch]$NoFrontend
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "backend\venv\Scripts\python.exe"
[System.Net.WebRequest]::DefaultWebProxy = New-Object System.Net.WebProxy   # never route local calls via a proxy

if ($Model -notmatch '^[A-Za-z0-9._:/-]+$') { throw "Invalid model name." }
if (-not (Test-Path $python)) { throw "Python venv not found at $python" }

function Get-Listener([int]$Port) { Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue }
function Test-LocalOnly([int]$Port, [string]$Name) {
    $addrs = @(Get-Listener $Port | ForEach-Object { $_.LocalAddress } | Sort-Object -Unique)
    if ($addrs | Where-Object { $_ -in @("0.0.0.0", "::") }) {
        Write-Warning "$Name (port $Port) listens on ALL interfaces ($($addrs -join ', ')). Restrict it to 127.0.0.1."
    }
}
function Wait-Http([string]$Url, [int]$Seconds = 90) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try { return Invoke-RestMethod -Uri $Url -TimeoutSec 5 } catch { Start-Sleep -Milliseconds 800 }
    }
    return $null
}

# 1. Ollama -------------------------------------------------------------------
if (Get-Listener 11434) {
    Write-Host "Ollama:   already running"
} else {
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) { throw "ollama is not on PATH - install Ollama first." }
    $env:OLLAMA_HOST = "127.0.0.1:11434"
    Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Minimized
    Write-Host "Ollama:   started on 127.0.0.1:11434"
}
$tags = Wait-Http "http://127.0.0.1:11434/api/tags" 30
Test-LocalOnly 11434 "Ollama"
if ($tags -and ($tags.models.name -contains $Model -or $tags.models.name -contains "$Model`:latest")) {
    Write-Host "Model:    $Model installed"
} else {
    Write-Warning "Model $Model is not installed (run: ollama pull $Model). Chat will use deterministic template answers."
}

# 2. Backend ------------------------------------------------------------------
if (Get-Listener 8000) {
    Write-Host "Backend:  already running (restart it after recovery.py prepare-demo or a model change)"
} else {
    $cmd = "`$env:BLAST_OLLAMA_MODEL='$Model'; Set-Location '$root'; & '$python' backend\main.py"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd
    Write-Host "Backend:  starting on 127.0.0.1:8000 (model $Model)"
}

# 3. Frontend -----------------------------------------------------------------
if (-not $NoFrontend) {
    if (Get-Listener 5173) {
        Write-Host "Frontend: already running"
    } else {
        $fe = Join-Path $root "frontend"
        $install = if (Test-Path (Join-Path $fe "node_modules")) { "" } else { "npm install --no-audit --no-fund; " }
        Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$fe'; ${install}npm run dev"
        Write-Host "Frontend: starting on 127.0.0.1:5173"
    }
}

# 4. Verify -------------------------------------------------------------------
$health = Wait-Http "http://127.0.0.1:8000/api/health" 90
if (-not $health) { Write-Warning "Backend did not answer /api/health within 90 s - check its window."; exit 1 }
Test-LocalOnly 8000 "Backend"
$ai = Wait-Http "http://127.0.0.1:8000/api/ai/status" 20
Write-Host ""
Write-Host ("API health:  {0}  database={1}  confirmed entities={2}" -f $health.status, $health.database.available,
    ($health.analytics_mapping.confirmed_entities -join ","))
if ($health.status -ne "ok") {
    Write-Warning ("Degraded: db error='{0}' mapping error='{1}'. Run: python backend\recovery.py check" -f `
        $health.database.error, $health.analytics_mapping.error)
}
if ($ai) {
    Write-Host ("AI status:   reachable={0}  model={1}  available={2}  cloud_fallback={3}" -f $ai.reachable, $ai.model,
        $ai.model_available, $ai.cloud_fallback)
}
if (-not $NoFrontend) {
    if (Wait-Http "http://127.0.0.1:5173" 60) { Test-LocalOnly 5173 "Frontend"; Write-Host "Open:        http://127.0.0.1:5173" }
    else { Write-Warning "Frontend not reachable yet - check its window." }
}
