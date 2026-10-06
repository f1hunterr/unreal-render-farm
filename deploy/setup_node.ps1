<#
.SYNOPSIS
    One-click render node setup. Run through SETUP.bat (which asks for admin rights).

.DESCRIPTION
    1. Copies the package to C:\UnrealRenderFarm\node
    2. Finds Unreal Engine and records its path
    3. Installs Python 3.12 from the bundled installer if this machine has no Python 3.12
    4. Creates a private Python environment and installs the bundled dependencies (no internet needed)
    5. Installs the agent as an auto-starting, self-restarting task; opens port 5001 to the master only
    6. Checks the agent is running and registers this machine with the master
#>
param(
    [string]$InstallDir = 'C:\UnrealRenderFarm\node',
    # An artist's PC: renders only while nobody uses it (workstation mode, URF_NIMBY)
    [switch]$Workstation
)

$ErrorActionPreference = 'Stop'
$olderTasks = @()
$package = Split-Path -Parent $PSScriptRoot
$taskName = 'Unreal Render Farm Agent'
# Agents installed by older versions run render_agent_v2.py from another folder and task name
$olderScript = 'render_agent_v2.py'
New-Item -ItemType Directory -Force -Path 'C:\UnrealRenderFarm\logs' | Out-Null
Start-Transcript -Path 'C:\UnrealRenderFarm\logs\setup.log' -Append | Out-Null

function Step($message) { Write-Host "`n==> $message" -ForegroundColor Cyan }
function Ok($message) { Write-Host "    $message" -ForegroundColor Green }
function Warn($message) { Write-Host "    WARNING: $message" -ForegroundColor Yellow }

function Read-Settings($path) {
    $settings = @{}
    foreach ($line in Get-Content $path) {
        # older versions used another prefix for the same names (e.g. XYZ_UE_EXE): they count as URF_
        if ($line -match '^\s*[A-Z]+_([A-Z_]+)\s*=\s*(.*?)\s*$') { $settings["URF_$($Matches[1])"] = $Matches[2].Trim('"', "'") }
    }
    return $settings
}

function Set-Setting($path, $key, $value) {
    $found = $false
    $lines = foreach ($line in @(Get-Content $path)) {
        if (-not $found -and $line -match "^\s*#?\s*$key\s*=") { $found = $true; "$key=$value" }
        elseif ($found -and $line -match "^\s*$key\s*=") { }   # drop duplicate definitions
        else { $line }
    }
    $lines = @($lines)
    if (-not $found) { $lines += "$key=$value" }
    [IO.File]::WriteAllLines($path, [string[]]$lines, (New-Object Text.UTF8Encoding $false))
}

function Find-Unreal {
    $candidates = @()
    $manifest = 'C:\ProgramData\Epic\UnrealEngineLauncher\LauncherInstalled.dat'
    if (Test-Path $manifest) {
        try {
            (Get-Content $manifest -Raw | ConvertFrom-Json).InstallationList |
                Where-Object { $_.AppName -like 'UE_5*' } |
                ForEach-Object { $candidates += [pscustomobject]@{ Version = $_.AppName -replace '^UE_', ''; Dir = $_.InstallLocation } }
        } catch { }
    }
    foreach ($key in Get-ChildItem 'HKLM:\SOFTWARE\EpicGames\Unreal Engine' -ErrorAction SilentlyContinue) {
        $dir = (Get-ItemProperty $key.PSPath -ErrorAction SilentlyContinue).InstalledDirectory
        if ($dir) { $candidates += [pscustomobject]@{ Version = $key.PSChildName; Dir = $dir } }
    }
    foreach ($dir in Get-ChildItem 'C:\Program Files\Epic Games' -Directory -Filter 'UE_5*' -ErrorAction SilentlyContinue) {
        $candidates += [pscustomobject]@{ Version = $dir.Name -replace '^UE_', ''; Dir = $dir.FullName }
    }
    $found = $candidates |
        ForEach-Object { [pscustomobject]@{ Version = $_.Version; Exe = Join-Path $_.Dir 'Engine\Binaries\Win64\UnrealEditor-Cmd.exe' } } |
        Where-Object { Test-Path $_.Exe } |
        Sort-Object { if ($_.Version -eq '5.6') { [version]'99.0' } else { try { [version]$_.Version } catch { [version]'0.0' } } } -Descending
    return $found | Select-Object -First 1
}

function Find-Python312 {
    # Only a Python installed for all users: the agent runs as the desktop user, who can't use a Python
    # installed in the profile of the admin account that runs this setup
    $paths = @("$env:ProgramFiles\Python312\python.exe")
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        try { $paths = @((& $py.Source -3.12 -c 'import sys; print(sys.executable)' 2>$null)) + $paths } catch { }
    }
    return $paths | Where-Object { $_ -and (Test-Path $_) -and $_ -like "$env:ProgramFiles\*" } | Select-Object -First 1
}

function Show-AgentDiagnostics($port) {
    Write-Host "`n    --- Why the agent is not answering ---" -ForegroundColor Yellow
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) {
        $info = $task | Get-ScheduledTaskInfo
        Write-Host ("    Task state: {0}; runs as: {1}; last run: {2}; last result: 0x{3:X}" -f
            $task.State, $task.Principal.UserId, $info.LastRunTime, $info.LastTaskResult)
        $desktopUser = (Get-CimInstance Win32_ComputerSystem).UserName
        if ($desktopUser -and $task.Principal.UserId -notlike "*$($desktopUser.Split('\')[-1])") {
            Write-Host "    The task runs as $($task.Principal.UserId) but $desktopUser is logged on: it starts only when that user logs on." -ForegroundColor Yellow
        }
    }
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
        Write-Host "    Port $port is held by: $($owner.Name) (pid $($owner.ProcessId)) $($owner.CommandLine)"
    } else {
        Write-Host "    Nothing is listening on port $port."
    }
    foreach ($name in 'restarts', 'crash', 'console') {
        $file = "C:\UnrealRenderFarm\logs\farm_agent.$name.log"
        if ((Test-Path $file) -and (Get-Item $file).Length -gt 0) {
            Write-Host "    --- $file (last lines):"
            Get-Content $file -Tail 15 | ForEach-Object { Write-Host "      $_" }
        }
    }
}

try {
    # ------------------------------------------------------------------ 1. files
    Step "Installing files to $InstallDir"
    # An agent installed by an older version: one agent per PC, so it goes first (its settings are kept)
    $olderEnv = $null
    $olderDir = $null
    $olderTasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue |
        Where-Object { ($_.Actions | ForEach-Object { $_.Arguments }) -like "*$olderScript*" })
    foreach ($older in $olderTasks) {
        # paused, not removed: if this setup fails the older agent is switched back on (see catch)
        $olderDir = ($older.Actions | Select-Object -First 1).WorkingDirectory
        Stop-ScheduledTask -TaskName $older.TaskName -ErrorAction SilentlyContinue
        Disable-ScheduledTask -TaskName $older.TaskName | Out-Null
        Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
            Where-Object { $_.CommandLine -like "*$olderScript*" } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
        if ($olderDir -and (Test-Path $olderDir)) {
            $olderEnv = Get-ChildItem $olderDir -Filter '*farm.env' -File -ErrorAction SilentlyContinue | Select-Object -First 1
        }
        Start-Sleep -Seconds 2
        Ok "Paused the older agent '$($older.TaskName)' from $olderDir (removed once the new one runs)"
    }
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $taskName
        Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
            Where-Object { $_.CommandLine -like '*farm_agent.py*' } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
        Start-Sleep -Seconds 2
        Ok 'Stopped the previously installed agent (update)'
    }
    $envFile = Join-Path $InstallDir 'farm.env'
    $samePlace = (Resolve-Path $package).Path.TrimEnd('\') -ieq [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
    if (-not $samePlace) {
        & robocopy.exe $package $InstallDir /E /XD .venv /XF farm.env /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "Copying files to $InstallDir failed (robocopy code $LASTEXITCODE)" }
        $packageEnv = Join-Path $package 'farm.env'
        if (-not (Test-Path $envFile)) {
            Copy-Item $packageEnv $envFile
            if ($olderEnv) {
                # Keep what was set on this PC for the older agent (Unreal path, project folders, ...)
                $packageSettings = Read-Settings $packageEnv
                $old = Read-Settings $olderEnv.FullName
                foreach ($key in $old.Keys) { if (-not $packageSettings.ContainsKey($key)) { Set-Setting $envFile $key $old[$key] } }
                Ok "Carried over this PC's settings from $($olderEnv.FullName)"
            }
        } else {
            # Update: keep settings made on this node, take every setting the package defines
            $packageSettings = Read-Settings $packageEnv
            foreach ($key in $packageSettings.Keys) { Set-Setting $envFile $key $packageSettings[$key] }
        }
    }
    Get-ChildItem $InstallDir -Recurse -File | Where-Object { $_.FullName -notlike '*\.venv\*' } | Unblock-File
    Ok 'Files copied'

    if ($Workstation) {
        Set-Setting $envFile 'URF_NIMBY' '1'
        Ok 'Workstation mode: this PC renders only after 15 minutes without keyboard or mouse, and stops when you come back'
    }
    $settings = Read-Settings $envFile
    if (-not $settings.URF_FARM_TOKEN -or $settings.URF_FARM_TOKEN.Length -lt 16 -or $settings.URF_FARM_TOKEN -like 'change-me*') {
        throw "farm.env has no valid URF_FARM_TOKEN. Rebuild the package on the master (deploy\build_agent_package.ps1)."
    }

    # ------------------------------------------------------------------ 2. Unreal
    Step 'Looking for Unreal Engine'
    if ($settings.URF_UE_EXE -and (Test-Path $settings.URF_UE_EXE)) {
        Ok "Using configured $($settings.URF_UE_EXE)"
    } else {
        $unreal = Find-Unreal
        if ($unreal) {
            Set-Setting $envFile 'URF_UE_EXE' $unreal.Exe
            Ok "Found Unreal $($unreal.Version): $($unreal.Exe)"
        } else {
            Warn 'Unreal Engine 5 not found. Install it, or set URF_UE_EXE in farm.env, then run SETUP.bat again.'
        }
    }

    # ------------------------------------------------------------------ 3. Python
    Step 'Checking Python 3.12'
    $python = Find-Python312
    if (-not $python) {
        $installer = Get-ChildItem (Join-Path $InstallDir 'installers') -Filter 'python-3.12*-amd64.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $installer) { throw 'Python 3.12 is not installed and the package has no bundled installer.' }
        Write-Host '    Installing Python 3.12 (about a minute)...'
        $proc = Start-Process -FilePath $installer.FullName -Wait -PassThru -ArgumentList @(
            '/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'Include_test=0', 'Include_launcher=1', 'Shortcuts=0')
        if ($proc.ExitCode -notin 0, 3010) { throw "Python installer failed with exit code $($proc.ExitCode)" }  # 3010 = done, reboot later
        $python = Find-Python312
        if (-not $python) { throw 'Python 3.12 was installed but could not be found.' }
    }
    Ok "Python: $python"

    # ------------------------------------------------------------------ 4. dependencies
    Step 'Installing dependencies'
    $venvPython = Join-Path $InstallDir '.venv\Scripts\python.exe'
    if (-not (Test-Path $venvPython)) {
        & $python -m venv (Join-Path $InstallDir '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment' }
    }
    $wheels = Join-Path $InstallDir 'wheels'
    $requirements = Join-Path $InstallDir 'requirements.txt'
    $pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '-r', $requirements)
    if (Test-Path $wheels) {
        & $venvPython @pipArgs --no-index --find-links $wheels
        if ($LASTEXITCODE -ne 0) { Warn 'Bundled packages did not install; trying the internet'; & $venvPython @pipArgs }
    } else {
        & $venvPython @pipArgs
    }
    if ($LASTEXITCODE -ne 0) { throw 'Installing Python packages failed' }
    Ok 'Dependencies installed'

    # ------------------------------------------------------------------ 5. service
    Step 'Installing the agent as an automatic task'
    $settings = Read-Settings $envFile
    $port = if ($settings.URF_AGENT_PORT) { [int]$settings.URF_AGENT_PORT } else { 5001 }
    $busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($busy) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($busy.OwningProcess)"
        throw ("Port $port is already used by $($owner.Name) (pid $($owner.ProcessId)). Stop that program, " +
               "or set URF_AGENT_PORT in farm.env on every node AND the master, then run SETUP.bat again.")
    }
    $allowFrom = 'LocalSubnet'
    if ($settings.URF_MASTER_URL) {
        $allowFrom = ([uri]$settings.URF_MASTER_URL).Host
        if ($allowFrom -notmatch '^[0-9.]+$') {
            $ips = @([System.Net.Dns]::GetHostAddresses($allowFrom) | Where-Object { $_.AddressFamily -eq 'InterNetwork' } |
                ForEach-Object { $_.IPAddressToString })
            if (-not $ips) { throw "Could not find the IP address of the master '$allowFrom' (URF_MASTER_URL)" }
            $allowFrom = $ips -join ','
        }
    }
    & (Join-Path $InstallDir 'deploy\install_service.ps1') -Role Agent -Python $venvPython -AllowFrom $allowFrom

    # ------------------------------------------------------------------ 6. verify
    Step 'Checking the agent (the first start can take a minute while antivirus scans the new Python)'
    $healthy = $false
    foreach ($i in 1..90) {
        try { Invoke-RestMethod "http://127.0.0.1:$port/health" -TimeoutSec 2 | Out-Null; $healthy = $true; break } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $healthy) {
        Show-AgentDiagnostics $port
        throw 'The agent did not start within 90 seconds (details above).'
    }
    Ok "Agent is running on port $port"

    if ($settings.URF_MASTER_URL) {
        try {
            $ip = (Find-NetRoute -RemoteIPAddress ([uri]$settings.URF_MASTER_URL).Host | Select-Object -First 1).IPAddress
            $body = @{ name = $env:COMPUTERNAME; ip = $ip } | ConvertTo-Json
            $reply = Invoke-RestMethod "$($settings.URF_MASTER_URL)/register-node" -Method Post -Body $body `
                -ContentType 'application/json' -Headers @{ 'X-Farm-Token' = $settings.URF_FARM_TOKEN } -TimeoutSec 5
            Ok "Registered with the master as $env:COMPUTERNAME ($ip): $($reply.status)"
            if ($reply.reachable -eq $true) {
                Ok 'The master can reach this node'
            } elseif ($reply.reachable -eq $false) {
                Warn ("The master cannot reach this node on port $port. Check other firewall/antivirus software " +
                      "on this PC, and that $ip is the address the master should use.")
            }
        } catch {
            Warn "Could not register with $($settings.URF_MASTER_URL): $($_.Exception.Message). The agent keeps retrying every minute."
        }
    } else {
        Warn 'URF_MASTER_URL is not set: add this machine in the dashboard Node Registry by hand.'
    }

    # The new agent is running: the older agent's task and firewall rule can go for good
    foreach ($older in $olderTasks) {
        Unregister-ScheduledTask -TaskName $older.TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Get-NetFirewallRule -DisplayName $older.TaskName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    }
    $olderTasks = @()
    # ...and its files, but only a packaged node install (a folder named "node" holding the older agent):
    # never a code checkout, a master, a drive root or the new install
    $isOlderInstall = $olderDir -and (Test-Path (Join-Path $olderDir "agent\$olderScript")) -and
        ((Split-Path -Leaf ([IO.Path]::GetFullPath($olderDir).TrimEnd('\'))) -eq 'node') -and
        -not (Test-Path (Join-Path $olderDir '.git')) -and -not (Test-Path (Join-Path $olderDir 'master')) -and
        ([IO.Path]::GetFullPath($olderDir).TrimEnd('\') -ine [IO.Path]::GetFullPath($InstallDir).TrimEnd('\'))
    if ($olderDir -and (Test-Path $olderDir) -and -not $isOlderInstall) {
        Warn "Left the older agent's folder $olderDir in place (not a standard install); delete it yourself if unused."
    }
    if ($isOlderInstall) {
        $olderRoot = Split-Path -Parent ([IO.Path]::GetFullPath($olderDir).TrimEnd('\'))
        $ownParts = @((Split-Path -Leaf $olderDir), 'logs', 'agent')
        $onlyOurs = -not (Get-ChildItem $olderRoot -Force -ErrorAction SilentlyContinue | Where-Object { $ownParts -notcontains $_.Name })
        Remove-Item $olderDir -Recurse -Force -ErrorAction SilentlyContinue
        if ($onlyOurs -and $olderRoot.TrimEnd('\') -notmatch '^[A-Za-z]:$') {
            # its logs sat next to it (<root>\logs, <root>\agent\logs); remove the root once it is empty
            foreach ($part in 'logs', 'agent') { Remove-Item (Join-Path $olderRoot $part) -Recurse -Force -ErrorAction SilentlyContinue }
            if (-not (Get-ChildItem $olderRoot -Force -ErrorAction SilentlyContinue)) { Remove-Item $olderRoot -Force -ErrorAction SilentlyContinue }
        }
        Ok "Removed the older install in $olderDir"
    }

    Write-Host "`nDone. This render node is ready." -ForegroundColor Green
    Write-Host 'The agent starts automatically when this user logs on. For unattended rendering after a'
    Write-Host 'reboot, set this machine to log on automatically (e.g. Sysinternals Autologon).'
} catch {
    foreach ($older in @($olderTasks)) {
        # setup failed: switch the older agent back on so this PC keeps rendering
        Enable-ScheduledTask -TaskName $older.TaskName -ErrorAction SilentlyContinue | Out-Null
        Start-ScheduledTask -TaskName $older.TaskName -ErrorAction SilentlyContinue
        Write-Host "    The older agent '$($older.TaskName)' was switched back on." -ForegroundColor Yellow
    }
    Write-Host "`nSETUP FAILED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Full log: C:\UnrealRenderFarm\logs\setup.log'
    exit 1
} finally {
    Stop-Transcript | Out-Null
}
