$ErrorActionPreference = 'Stop'
$projectPath = $PSScriptRoot
foreach ($name in @('subtitles', 'ollama')) {
    $pidPath = Join-Path $projectPath ".runtime/$name.pid"
    if (Test-Path -LiteralPath $pidPath) {
        $serviceId = [int](Get-Content -LiteralPath $pidPath)
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$serviceId" -ErrorAction SilentlyContinue
        if ($process -and $process.ExecutablePath -and $process.ExecutablePath.StartsWith($projectPath + '\', [StringComparison]::OrdinalIgnoreCase)) {
            Stop-Process -Id $serviceId -ErrorAction SilentlyContinue
            Write-Host "Stopped project $name service."
        }
        Remove-Item -LiteralPath $pidPath -Force
    }
}
