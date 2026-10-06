<#
.SYNOPSIS
    Removes the scheduled task and firewall rule created by install_service.ps1 and stops the process.
#>
#Requires -RunAsAdministrator
param(
    [Parameter(Mandatory)][ValidateSet('Agent', 'Master')][string]$Role
)

$ErrorActionPreference = 'Stop'
$taskName = "Unreal Render Farm $Role"
$scriptName = if ($Role -eq 'Agent') { 'farm_agent.py' } else { 'farm_master.py' }

# This task, and one an older version installed (found by the script it runs)
$legacyScript = if ($Role -eq 'Agent') { 'render_agent_v2.py' } else { 'studio_dash_v2.py' }
$tasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
    $_.TaskName -eq $taskName -or (($_.Actions | ForEach-Object { $_.Arguments }) -like "*$legacyScript*") })
foreach ($task in $tasks) {
    Stop-ScheduledTask -TaskName $task.TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $task.TaskName -Confirm:$false
    Get-NetFirewallRule -DisplayName $task.TaskName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
}

# Stopping the task ends the restart loop; also stop the Python process it started
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like "*$scriptName*" -or $_.CommandLine -like "*$legacyScript*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

Write-Host "Removed '$taskName'. (A render already running in Unreal is not stopped.)"
