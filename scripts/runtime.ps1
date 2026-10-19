$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimePath = Join-Path $ProjectRoot '.runtime'

function Install-VerifiedReleaseAsset {
    param([string]$Repository, [string]$Tag, [string]$AssetName, [string]$Destination)
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repository/releases/$Tag"
    $asset = $release.assets | Where-Object { $_.name -eq $AssetName } | Select-Object -First 1
    if (-not $asset -or -not $asset.digest -or -not $asset.digest.StartsWith('sha256:')) {
        throw "No SHA256 digest found for $Repository / $AssetName."
    }
    $archive = Join-Path $RuntimePath $AssetName
    $expected = $asset.digest.Substring(7)
    if (-not (Test-Path -LiteralPath $archive) -or (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) {
        Write-Host "Downloading $AssetName..."
        $previousProgress = $ProgressPreference
        try {
            $ProgressPreference = 'SilentlyContinue'
            Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $archive -UseBasicParsing
        } finally { $ProgressPreference = $previousProgress }
    }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) {
        throw "SHA256 verification failed for $AssetName."
    }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Expand-Archive -LiteralPath $archive -DestinationPath $Destination -Force
}

function Start-ProjectOllama {
    $env:OLLAMA_HOST = '127.0.0.1:11434'
    $env:OLLAMA_MODELS = Join-Path $RuntimePath 'ollama-models'
    $env:OLLAMA_NO_CLOUD = '1'
    $env:OLLAMA_CONTEXT_LENGTH = '2048'
    $env:OLLAMA_MAX_LOADED_MODELS = '1'
    $ollamaPath = Join-Path $RuntimePath 'ollama/ollama.exe'
    if (-not (Test-Path -LiteralPath $ollamaPath)) { throw 'Ollama is missing. Run setup.ps1 first.' }
    $listener = Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        foreach ($entry in @($listener)) {
            $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($entry.OwningProcess)" -ErrorAction SilentlyContinue
            if ($entry.LocalAddress -ne '127.0.0.1' -or -not $owner -or $owner.ExecutablePath -ne $ollamaPath) {
                throw 'Port 11434 belongs to another Ollama/service. Close it before starting this project.'
            }
        }
    }
    if (-not $listener) {
        $process = Start-Process -FilePath $ollamaPath -ArgumentList 'serve' -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $RuntimePath 'ollama-server.log') `
            -RedirectStandardError (Join-Path $RuntimePath 'ollama-server.err.log')
        $process.Id | Set-Content -LiteralPath (Join-Path $RuntimePath 'ollama.pid')
    }
    $deadline = (Get-Date).AddSeconds(30)
    do {
        try {
            $null = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/version' -TimeoutSec 2
            return
        } catch { Start-Sleep -Milliseconds 500 }
    } while ((Get-Date) -lt $deadline)
    throw 'Ollama did not start. See .runtime/ollama-server.err.log.'
}
