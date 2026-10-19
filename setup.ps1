param([switch]$SkipModels, [string]$TranslationModel)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts/runtime.ps1')
Set-Location -LiteralPath $ProjectRoot
New-Item -ItemType Directory -Path $RuntimePath -Force | Out-Null

$pythonPath = Join-Path $ProjectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $uvDirectory = Join-Path $RuntimePath 'uv'
    $uvPath = Join-Path $uvDirectory 'uv.exe'
    if (-not (Test-Path -LiteralPath $uvPath)) {
        Install-VerifiedReleaseAsset -Repository 'astral-sh/uv' -Tag 'latest' `
            -AssetName 'uv-x86_64-pc-windows-msvc.zip' -Destination $uvDirectory
    }
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $RuntimePath 'python'
    $env:UV_PYTHON_BIN_DIR = Join-Path $RuntimePath 'python-bin'
    $env:UV_PYTHON_PREFERENCE = 'only-managed'
    & $uvPath python install 3.12
    if ($LASTEXITCODE -ne 0) { throw 'Python installation failed.' }
    & $uvPath venv --python 3.12 --seed .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed.' }
}

Write-Host 'Installing the tested local speech and GPU dependencies...'
& $pythonPath -m pip install -r requirements-lock.txt --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }

$ollamaDirectory = Join-Path $RuntimePath 'ollama'
if (-not (Test-Path -LiteralPath (Join-Path $ollamaDirectory 'ollama.exe'))) {
    Install-VerifiedReleaseAsset -Repository 'ollama/ollama' -Tag 'tags/v0.40.6' `
        -AssetName 'ollama-windows-amd64.zip' -Destination $ollamaDirectory
}
Start-ProjectOllama
$configureArguments = @('-m', 'scripts.configure')
if ($TranslationModel) { $configureArguments += @('--translation-model', $TranslationModel) }
& $pythonPath @configureArguments
if ($LASTEXITCODE -ne 0) { throw 'Local configuration failed.' }
$selectedModel = (& $pythonPath -m scripts.configure --print-model).Trim()
if ($LASTEXITCODE -ne 0 -or -not $selectedModel) { throw 'Could not read the local model selection.' }
if (-not $SkipModels) {
    Write-Host 'Downloading the local translation model (first run only)...'
    & (Join-Path $ollamaDirectory 'ollama.exe') pull $selectedModel
    if ($LASTEXITCODE -ne 0) { throw 'Translation model download failed.' }
}

if (-not $SkipModels) {
    & $pythonPath -m scripts.prepare_models
    if ($LASTEXITCODE -ne 0) { throw 'Speech model preparation failed.' }
}
Write-Host 'Setup complete. Run start.bat, then load the extension folder in Chrome.'
