param([switch]$Uninstall)
$ErrorActionPreference = 'Stop'
$startupFolder = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupFolder 'CodexMonitor.lnk'
if ($Uninstall) {
    if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath
    }
    Write-Host 'Codex Monitor autostart removed. The current session is unchanged.'
    return
}
$exePath = Join-Path $PSScriptRoot 'dist\CodexMonitor.exe'
if (-not (Test-Path -LiteralPath $exePath)) { throw "Missing EXE: $exePath" }
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $exePath
$shortcut.Arguments = '--follow-codex'
$shortcut.WorkingDirectory = Split-Path -Parent $exePath
$shortcut.Description = 'Show the activity monitor while the Codex desktop app is running.'
$shortcut.Save()
Write-Host "Installed: $shortcutPath"
Start-Process -FilePath $exePath -ArgumentList '--follow-codex' -WindowStyle Hidden
