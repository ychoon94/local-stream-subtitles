$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts/runtime.ps1')
$script:TestAddress = '127.0.0.1'
$script:TestExecutable = Join-Path $RuntimePath 'ollama/ollama.exe'
function Get-NetTCPConnection { [PSCustomObject]@{LocalAddress=$script:TestAddress; OwningProcess=42} }
function Get-CimInstance { [PSCustomObject]@{ExecutablePath=$script:TestExecutable} }
function Invoke-RestMethod { return @{version='test'} }
function Test-Path {
    param([string]$LiteralPath)
    if ($LiteralPath -eq (Join-Path $RuntimePath 'ollama/ollama.exe')) { return $true }
    return Microsoft.PowerShell.Management\Test-Path -LiteralPath $LiteralPath
}

Start-ProjectOllama
Write-Host 'PASS: accepts project-local loopback Ollama.'
$script:TestExecutable = 'C:\Unrelated\ollama.exe'
$rejected = $false
try { Start-ProjectOllama } catch { $rejected = $_.Exception.Message -like '*another Ollama*' }
if (-not $rejected) { throw 'Unrelated listener was accepted.' }
Write-Host 'PASS: rejects unrelated service on Ollama port.'
$script:TestExecutable = Join-Path $RuntimePath 'ollama/ollama.exe'
$script:TestAddress = '0.0.0.0'
$rejected = $false
try { Start-ProjectOllama } catch { $rejected = $_.Exception.Message -like '*another Ollama*' }
if (-not $rejected) { throw 'Non-loopback listener was accepted.' }
Write-Host 'PASS: rejects non-loopback binding.'
