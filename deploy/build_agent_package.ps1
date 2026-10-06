<#
.SYNOPSIS
    Builds the copy-and-run render node package (dist\UnrealRenderFarm_agent + .zip). Run on the master.

.DESCRIPTION
    The package holds the agent, the setup scripts, the Python 3.12 installer and every dependency as
    offline wheels, plus a farm.env with this farm's token and master address. On a render
    node you copy the folder and double-click SETUP.bat. No internet is needed on the node.

.EXAMPLE
    .\deploy\build_agent_package.ps1 -MasterUrl http://192.168.1.5:5000
#>
param(
    # Master address as render nodes reach it. Default: this machine's LAN IP, port 5000.
    [string]$MasterUrl,
    # Any Python 3.9+ with pip; only used to download the wheels.
    [string]$Python = 'python',
    [string]$OutDir
)

$ErrorActionPreference = 'Stop'
$source = Split-Path -Parent $PSScriptRoot                 # the project folder
$repo = $source                                # dist goes in the project folder (git-ignored)
if (-not $OutDir) { $OutDir = Join-Path $repo 'dist\UnrealRenderFarm_agent' }
$cache = Join-Path $repo 'dist\cache'
$pythonVersion = '3.12.10'
$pythonInstaller = "python-$pythonVersion-amd64.exe"

$masterEnv = Join-Path $source 'farm.env'
if (-not (Test-Path $masterEnv)) { throw "No $masterEnv - the package takes the farm token from the master's settings." }
$tokenLine = Get-Content $masterEnv | Where-Object { $_ -match '^\s*URF_FARM_TOKEN\s*=\s*\S{16,}' } | Select-Object -First 1
if (-not $tokenLine) { throw "URF_FARM_TOKEN missing or shorter than 16 characters in $masterEnv" }
if ($tokenLine -match '=\s*change-me') { throw "URF_FARM_TOKEN in $masterEnv is still the example value: set your own secret" }
$tokenLine = $tokenLine.Trim()

if (-not $MasterUrl) {
    $ip = Get-NetIPAddress -AddressFamily IPv4 -PrefixOrigin Dhcp, Manual |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.InterfaceAlias -notlike 'vEthernet*' } |
        Select-Object -First 1 -ExpandProperty IPAddress
    if (-not $ip) { throw 'Could not detect this machine''s LAN IP; pass -MasterUrl http://<ip>:5000' }
    $MasterUrl = "http://${ip}:5000"
}
$MasterUrl = $MasterUrl.TrimEnd('/')

Write-Host "Building render node package for master $MasterUrl"
# Empty rather than delete the folder, so an Explorer window open on it doesn't break the build
if (Test-Path $OutDir) { Get-ChildItem $OutDir -Force | Remove-Item -Recurse -Force }
New-Item -ItemType Directory -Force -Path "$OutDir\agent\unreal", "$OutDir\deploy", "$OutDir\tools", "$OutDir\installers", "$OutDir\wheels", $cache | Out-Null

Copy-Item "$source\agent\farm_agent.py" "$OutDir\agent\"
Copy-Item "$source\agent\unreal\*.py" "$OutDir\agent\unreal\"   # Movie Render Queue executor (runs inside Unreal)
Copy-Item "$source\deploy\install_service.ps1", "$source\deploy\uninstall_service.ps1", "$source\deploy\run_forever.ps1", "$source\deploy\setup_node.ps1" "$OutDir\deploy\"
Copy-Item "$source\deploy\node\*" $OutDir
Copy-Item "$source\tools\check_ue_log.py" "$OutDir\tools\"
Copy-Item "$source\requirements.txt" $OutDir

# Python installer (cached between builds)
$cachedInstaller = Join-Path $cache $pythonInstaller
if (-not (Test-Path $cachedInstaller)) {
    Write-Host "Downloading $pythonInstaller..."
    Invoke-WebRequest "https://www.python.org/ftp/python/$pythonVersion/$pythonInstaller" -OutFile $cachedInstaller -UseBasicParsing
}
$signature = Get-AuthenticodeSignature $cachedInstaller
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike '*Python Software Foundation*') {
    Remove-Item $cachedInstaller -Force
    throw "Downloaded Python installer is not validly signed by the Python Software Foundation ($($signature.Status))"
}
Copy-Item $cachedInstaller "$OutDir\installers\"

# Dependencies as Windows wheels for Python 3.12 - the node installs them without internet
Write-Host 'Downloading dependency wheels...'
& $Python -m pip download --disable-pip-version-check --quiet -r "$source\requirements.txt" -d "$OutDir\wheels" `
    --only-binary=:all: --platform win_amd64 --python-version 3.12 --implementation cp
if ($LASTEXITCODE -ne 0) { throw 'pip download failed' }

$settings = @"
# Unreal Render Farm - render node settings (built $(Get-Date -Format 'yyyy-MM-dd HH:mm') for this farm)
# Keep private: the farm token lets a machine control render nodes.
$tokenLine
URF_MASTER_URL=$MasterUrl

# Optional: folders projects must be under (; separated). Required for network paths like \\server\share.
# URF_PROJECT_ROOTS=D:\Projects;\\server\share\Projects

# Optional: only if SETUP.bat can't find Unreal Engine by itself
# URF_UE_EXE=C:\Program Files\Epic Games\UE_5.6\Engine\Binaries\Win64\UnrealEditor-Cmd.exe
"@
[IO.File]::WriteAllText("$OutDir\farm.env", ($settings -replace "`r?`n", "`r`n"), (New-Object Text.UTF8Encoding $false))

$zip = "$OutDir.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path "$OutDir\*" -DestinationPath $zip

$size = [math]::Round(((Get-ChildItem $OutDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "`nPackage ready ($size MB):"
Write-Host "  Folder: $OutDir"
Write-Host "  Zip:    $zip"
Write-Host 'Copy either to a render node and double-click SETUP.bat.'
