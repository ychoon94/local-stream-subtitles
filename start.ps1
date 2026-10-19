param([switch]$Background)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts/runtime.ps1')
Set-Location -LiteralPath $ProjectRoot
$pythonPath = Join-Path $ProjectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Run setup.ps1 first.' }
if (-not (Test-Path -LiteralPath (Join-Path $RuntimePath 'settings.json'))) { throw 'Run setup.ps1 first.' }
Start-ProjectOllama
if (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Port 8765 is already in use. The subtitle service may already be running.'
}
$env:HF_HUB_OFFLINE = '1'
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
$process = Start-Process -FilePath $pythonPath -ArgumentList '-m', 'backend' -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $RuntimePath 'subtitle-service.log') `
    -RedirectStandardError (Join-Path $RuntimePath 'subtitle-service.err.log')
$process.Id | Set-Content -LiteralPath (Join-Path $RuntimePath 'subtitles.pid')
$ready = $false
try {
    Write-Host 'Warming local GPU models (first start can take a few minutes)...'
    $deadline = (Get-Date).AddSeconds(240)
    do {
        if ($process.HasExited) { throw 'Subtitle service failed. See .runtime/subtitle-service.err.log.' }
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' -TimeoutSec 2
            if ($health.ok) { $ready = $true; break }
        } catch { Start-Sleep -Milliseconds 500 }
    } while ((Get-Date) -lt $deadline)
    if (-not $ready) { throw 'Service startup timed out. See .runtime/subtitle-service.err.log.' }
    Write-Host 'Local subtitles ready. Use the extension on a YouTube video.'
    if (-not $Background) {
        Write-Host 'Keep this window open. Press Ctrl+C to stop the project services.'
        Wait-Process -Id $process.Id
    }
} finally {
    if (-not $Background -or -not $ready) {
        & (Join-Path $ProjectRoot 'stop.ps1')
    }
}
