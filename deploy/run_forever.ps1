<#
.SYNOPSIS
    Keeps a Unreal Render Farm process running: restarts it 10 seconds after it exits.
    Started by the scheduled task that install_service.ps1 creates.
#>
param(
    [Parameter(Mandatory)][string]$Python,
    [Parameter(Mandatory)][string]$Script,
    [string]$LogDir = 'C:\UnrealRenderFarm\logs'
)

$ErrorActionPreference = 'Continue'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$name = [IO.Path]::GetFileNameWithoutExtension($Script)
$restartLog = Join-Path $LogDir "$name.restarts.log"
$env:PYTHONUNBUFFERED = '1'

while ($true) {
    # Keep the previous run's console output for post-mortems
    $out = Join-Path $LogDir "$name.console.log"
    $err = Join-Path $LogDir "$name.crash.log"
    foreach ($file in @($out, $err)) {
        if (Test-Path $file) { Move-Item -Path $file -Destination "$file.prev" -Force }
    }

    Add-Content -Path $restartLog -Value "$(Get-Date -Format s) starting $Script"
    $proc = Start-Process -FilePath $Python -ArgumentList "`"$Script`"" `
        -WorkingDirectory (Split-Path -Parent $Script) `
        -RedirectStandardOutput $out -RedirectStandardError $err `
        -NoNewWindow -PassThru -Wait
    Add-Content -Path $restartLog -Value "$(Get-Date -Format s) exited with code $($proc.ExitCode); restarting in 10s (see $err)"
    Start-Sleep -Seconds 10
}
