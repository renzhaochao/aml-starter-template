[CmdletBinding()]
param([string]$Python = "", [switch]$SkipInstall)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $projectRoot
if (-not $Python) { $Python = Join-Path $projectRoot ".venv\Scripts\python.exe" }
if (-not (Test-Path -LiteralPath $Python)) { throw "Python not found: $Python" }
& $Python -c "import sys,struct; assert sys.platform == 'win32' and struct.calcsize('P') == 8, 'Build requires Windows x64 Python'"
if ($LASTEXITCODE -ne 0) { throw "Windows x64 Python is required" }
if (-not $SkipInstall) {
    & $Python -m pip install -r requirements-lock.txt -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }
}
& $Python -m PyInstaller --noconfirm --clean --distpath dist/windows --workpath build/windows packaging/windows/aml-server.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
$releaseDirectory = Join-Path $projectRoot "dist\AML-Memory-Server-1.0.0-windows-x64"
# Rebuild only an untouched release directory; preserve any deployment data.
if (Test-Path -LiteralPath $releaseDirectory) {
    if ((Test-Path -LiteralPath (Join-Path $releaseDirectory '.env')) -or
        (Test-Path -LiteralPath (Join-Path $releaseDirectory 'data'))) {
        throw "Release directory contains configuration or data; preserve it before rebuilding: $releaseDirectory"
    }
}
New-Item -ItemType Directory -Force -Path $releaseDirectory | Out-Null
Copy-Item -LiteralPath "dist\windows\aml-server.exe" -Destination $releaseDirectory
foreach ($fileName in @('start.cmd', '.env.example', 'README-Windows.md')) {
    Copy-Item -LiteralPath (Join-Path $projectRoot "packaging\windows\$fileName") -Destination $releaseDirectory
}
& $Python scripts/write_distribution_notices.py $releaseDirectory
if ($LASTEXITCODE -ne 0) { throw "License notice generation failed" }
$zipPath = "$releaseDirectory.zip"
# Explicit allowlist prevents accidentally packaging databases, keys or logs.
$releaseFiles = @('aml-server.exe', 'start.cmd', '.env.example', 'README-Windows.md', 'THIRD-PARTY-NOTICES.txt') | ForEach-Object { Join-Path $releaseDirectory $_ }
Compress-Archive -LiteralPath $releaseFiles -DestinationPath $zipPath -Force
$digest = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText("$zipPath.sha256", "$digest  $(Split-Path $zipPath -Leaf)`r`n", [System.Text.Encoding]::ASCII)
& $Python scripts/verify_windows_package.py $zipPath
if ($LASTEXITCODE -ne 0) { throw "Packaged EXE verification failed" }
Write-Host "Package: $zipPath"
Write-Host "SHA256: $digest"
