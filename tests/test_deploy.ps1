<#
.SYNOPSIS
    Checks the risky parts of the setup scripts in throw-away folders (run as an administrator):
    the install-folder permissions and the guard that decides whether an older install may be deleted.
    powershell -ExecutionPolicy Bypass -File tests\test_deploy.ps1
#>
$ErrorActionPreference = 'Stop'
$deploy = Join-Path (Split-Path -Parent $PSScriptRoot) 'deploy'
$failures = 0
function Check($name, $ok) {
    if ($ok) { Write-Host "ok   $name" -ForegroundColor Green } else { Write-Host "FAIL $name" -ForegroundColor Red; $script:failures++ }
}
function Block($file, $from, $to) {
    $src = Get-Content (Join-Path $deploy $file) -Raw
    $start = $src.IndexOf($from)
    if ($start -lt 0 -or $src.IndexOf($to) -lt $start) { throw "Block not found in $file (the script changed?)" }
    [scriptblock]::Create($src.Substring($start, $src.IndexOf($to) - $start))
}

# --- install folder: admins change the program files, users only read them, the agent's user writes logs
$acl = Block 'install_service.ps1' '$farmDir = Split-Path -Parent $root' '# Secrets: readable only'
$base = Join-Path $env:TEMP "urf-acl-$PID"
New-Item -ItemType Directory -Force "$base\node\agent" | Out-Null
Set-Content "$base\node\agent\farm_agent.py" 'x'
$root = "$base\node"; $Role = 'Agent'; $user = "$env:USERDOMAIN\$env:USERNAME"
. $acl
$fileAcl = (icacls "$base\node\agent\farm_agent.py") -join ' '
Check 'program files: users can only read' ($fileAcl -match 'BUILTIN\\Users:\(I\)\(RX\)' -and $fileAcl -notmatch 'Authenticated Users')
Check 'logs: the agent user can write' (((icacls "$base\logs") -join ' ') -match [regex]::Escape("${user}:(OI)(CI)(M)"))
Remove-Item $base -Recurse -Force

# --- an older install is deleted only when it is a packaged node folder
$guard = Block 'setup_node.ps1' '    $isOlderInstall =' '    if ($olderDir -and (Test-Path $olderDir) -and -not $isOlderInstall)'
$olderScript = 'render_agent_v2.py'; $InstallDir = 'C:\UnrealRenderFarm\node'
$cases = @(
    @{ Name = 'packaged node folder is removed'; Leaf = 'node'; Git = $false; Expect = $true },
    @{ Name = 'code checkout is kept'; Leaf = 'Unreal-Farm'; Git = $true; Expect = $false },
    @{ Name = 'node folder with .git is kept'; Leaf = 'node'; Git = $true; Expect = $false })
foreach ($case in $cases) {
    $dir = Join-Path $env:TEMP "urf-guard-$PID\$($case.Leaf)"
    New-Item -ItemType Directory -Force "$dir\agent" | Out-Null
    Set-Content "$dir\agent\$olderScript" 'x'
    if ($case.Git) { New-Item -ItemType Directory -Force "$dir\.git" | Out-Null }
    $olderDir = $dir
    . $guard
    Check $case.Name ([bool]$isOlderInstall -eq $case.Expect)
    Remove-Item (Split-Path $dir) -Recurse -Force
}

if ($failures) { Write-Host "$failures check(s) failed" -ForegroundColor Red; exit 1 }
Write-Host 'All deploy checks passed' -ForegroundColor Green
