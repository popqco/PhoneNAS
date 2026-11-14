$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

Write-Host '== install dependencies =='
python -m pip install -r requirements.txt

Write-Host '== build with PyInstaller =='
python -m PyInstaller --noconfirm --clean build/PhoneNAS.spec

Write-Host '== prepare portable package =='
Copy-Item build/portable.flag dist/PhoneNAS/portable.flag -Force
Copy-Item README.md dist/PhoneNAS/README.md -Force
Copy-Item build/LICENSES.md dist/PhoneNAS/LICENSES.md -Force
if (Test-Path dist/PhoneNAS-portable.zip) { Remove-Item dist/PhoneNAS-portable.zip -Force }
Compress-Archive -Path dist/PhoneNAS/* -DestinationPath dist/PhoneNAS-portable.zip -Force
Write-Host 'portable zip: dist\PhoneNAS-portable.zip'

# The unpacked dist folder should use %APPDATA% like an installed copy,
# so drop the portable flag before compiling the installer.
Remove-Item dist/PhoneNAS/portable.flag -Force -ErrorAction SilentlyContinue

Write-Host '== build installer =='
$isccCandidates = @(
    (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
)
$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
    & $iscc build/installer.iss
    Write-Host 'installer: dist\PhoneNAS-Setup-1.0.0.exe'
} else {
    Write-Warning 'Inno Setup 未安装，跳过安装包。可运行: winget install JRSoftware.InnoSetup'
}
