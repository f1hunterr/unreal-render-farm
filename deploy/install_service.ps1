<#
.SYNOPSIS
    Installs the render farm agent or master to start automatically and restart if it stops.

.DESCRIPTION
    Registers a scheduled task that runs run_forever.ps1, locks down farm.env, and opens the
    service port in Windows Firewall for the given source network only.

    Agent default trigger is "Logon": Unreal renders need the GPU of an interactive desktop session,
    so set the render user to log on automatically (e.g. Sysinternals Autologon).
    Master default trigger is "Startup": runs as SYSTEM at boot, no logon needed.

.EXAMPLE
    .\install_service.ps1 -Role Agent -AllowFrom 192.168.1.0/24
.EXAMPLE
    .\install_service.ps1 -Role Master -AllowFrom 192.168.1.0/24
#>
#Requires -RunAsAdministrator
param(
    [Parameter(Mandatory)][ValidateSet('Agent', 'Master')][string]$Role,
    [string]$Python,
    # Who may connect to the port, e.g. 192.168.1.0/24. Default: the local subnet only.
    [string]$AllowFrom = 'LocalSubnet',
    [ValidateSet('Logon', 'Startup')][string]$Trigger,
    # Account the Logon task runs as. Default: the user logged on at this desktop, which can differ
    # from the account that approved the admin prompt.
    [string]$RunAsUser
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root 'farm.env'

if (-not $Python) {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $cmd -or $cmd.Source -like '*WindowsApps*') {
        throw 'Python not found. Install Python 3.9+ and pass -Python C:\path\to\python.exe'
    }
    $Python = $cmd.Source
}
if (-not (Test-Path $envFile)) {
    throw "Missing $envFile. Copy farm.env.example to farm.env and fill in the secrets first."
}
if (-not $Trigger) { $Trigger = if ($Role -eq 'Agent') { 'Logon' } else { 'Startup' } }

# Read the configured port from farm.env (defaults 5001 agent / 5000 master)
$settingsText = Get-Content $envFile -Raw
$portKey = if ($Role -eq 'Agent') { 'URF_AGENT_PORT' } else { 'URF_MASTER_PORT' }
$port = if ($Role -eq 'Agent') { 5001 } else { 5000 }
if ($settingsText -match "(?m)^\s*$portKey\s*=\s*(\d+)") { $port = [int]$Matches[1] }

$script = if ($Role -eq 'Agent') { Join-Path $root 'agent\farm_agent.py' } else { Join-Path $root 'master\farm_master.py' }
$runner = Join-Path $PSScriptRoot 'run_forever.ps1'
$taskName = "Unreal Render Farm $Role"
if (-not $RunAsUser) {
    # Owner of the desktop (explorer.exe) in this session; elevating with other credentials keeps the session
    $session = (Get-Process -Id $PID).SessionId
    $explorer = Get-CimInstance Win32_Process -Filter "Name = 'explorer.exe'" |
        Where-Object { $_.SessionId -eq $session } | Select-Object -First 1
    if ($explorer) {
        $owner = Invoke-CimMethod -InputObject $explorer -MethodName GetOwner
        if ($owner.User) { $RunAsUser = "$($owner.Domain)\$($owner.User)" }
    }
    if (-not $RunAsUser) { $RunAsUser = "$env:USERDOMAIN\$env:USERNAME" }
}
$user = $RunAsUser

# Re-installing: stop the old task and its Python process so the port is free
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $taskName
    $scriptName = Split-Path -Leaf $script
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like "*$scriptName*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
}

# Secrets: readable only by Administrators, SYSTEM and the account the task runs as
& icacls.exe $envFile /inheritance:r /grant:r 'Administrators:F' 'SYSTEM:F' "${user}:R" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icacls failed to restrict $envFile" }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $root -Argument (
    "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -Python `"$Python`" -Script `"$script`"")

if ($Trigger -eq 'Startup') {
    $taskTrigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
} else {
    $taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
}

$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $taskTrigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Get-NetFirewallRule -DisplayName $taskName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
# All network profiles: render PCs are often on a network Windows calls "Public", and the rule is
# already limited to -AllowFrom (the master), which is what keeps it safe.
New-NetFirewallRule -DisplayName $taskName -Direction Inbound -Protocol TCP -LocalPort $port `
    -RemoteAddress $AllowFrom -Action Allow -Profile Any | Out-Null

Start-ScheduledTask -TaskName $taskName

$runsAs = if ($Trigger -eq 'Startup') { 'SYSTEM' } else { $user }
Write-Host "Installed '$taskName' ($Trigger trigger, runs as $runsAs) running $script"
$profiles = (Get-NetConnectionProfile | ForEach-Object { "$($_.InterfaceAlias)=$($_.NetworkCategory)" }) -join ', '
Write-Host "Firewall: TCP $port allowed from $AllowFrom on all network types (this PC: $profiles)"
Write-Host "Logs: C:\UnrealRenderFarm\logs ($([IO.Path]::GetFileNameWithoutExtension($script)).*.log)"
