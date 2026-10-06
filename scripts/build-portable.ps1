<#
.SYNOPSIS
Builds the portable Windows distribution of csibridge-mcp.

.DESCRIPTION
Combines the official "embeddable" Python for Windows (a signed, install-free
python.exe) with csibridge-mcp and its dependencies, adds double-clickable
launchers, and zips the result. The zip needs no Python, uv, git or admin
rights on the machine it is unpacked on.

Run on Windows with a Python of the same minor version as -PythonVersion on
the PATH (pip is used to lay out the packages). CI does this automatically.

.EXAMPLE
uv build --wheel
scripts\build-portable.ps1 -Wheel (Get-ChildItem dist\*.whl).FullName
#>
param(
    [Parameter(Mandatory = $true)] [string]$Wheel,
    [string]$PythonVersion = "3.12.10",
    [string]$OutDir = "dist"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$stage = Join-Path $root "$OutDir\portable\csibridge-mcp"

$runner = & python -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
$wanted = ($PythonVersion -split '\.')[0..1] -join '.'
if ($runner -ne $wanted) {
    throw "The Python on PATH is $runner but the embeddable build is $wanted; they must match so the installed wheels fit."
}

if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

$embed = "python-$PythonVersion-embed-amd64.zip"
$download = Join-Path ([IO.Path]::GetTempPath()) $embed
if (-not (Test-Path $download)) {
    Write-Host "Downloading $embed"
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/$PythonVersion/$embed" -OutFile $download
}
Expand-Archive -Path $download -DestinationPath "$stage\python" -Force

# The embeddable build ignores Lib\site-packages until "import site" is enabled in its ._pth file.
$pth = Get-ChildItem "$stage\python\python*._pth" | Select-Object -First 1
(Get-Content $pth.FullName) -replace '^#\s*import site', 'import site' | Set-Content $pth.FullName

Write-Host "Installing $Wheel and its dependencies"
& python -m pip install --quiet --no-warn-script-location --target "$stage\python\Lib\site-packages" $Wheel
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Copy-Item "$PSScriptRoot\portable\*" -Destination $stage
Copy-Item "$root\LICENSE.md" -Destination $stage

# Smoke test: the packaged interpreter must be able to start the server.
& "$stage\python\python.exe" -m csibridge_mcp --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "The packaged csibridge-mcp does not start" }

$zip = Join-Path $root "$OutDir\csibridge-mcp-portable-win64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $stage -DestinationPath $zip
Write-Host "Built $zip"
