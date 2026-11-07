$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    python -m PyInstaller --noconfirm --onefile --windowed --noupx --name CodexMonitor --distpath dist --workpath build --specpath build codex_monitor.py
    if ($LASTEXITCODE -ne 0) { throw 'EXE build failed.' }
    Write-Host "Built: $PSScriptRoot\dist\CodexMonitor.exe"
} finally {
    Pop-Location
}
