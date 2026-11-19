param(
    [switch]$SkipDependencies
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
try {
    $version = (python -c 'from app.constants import APP_VERSION; print(APP_VERSION)').Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Could not read application version.' }

    if (-not $SkipDependencies) {
        Write-Host '== install dependencies =='
        python -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    }

    $vendorHashes = @{
        'vendor/rclone.exe' = 'acd80683e23151e93bbe0d6ffa74d26e542ec510332f630beecb9944f8d1a623'
        'vendor/winfsp.msi' = '073a70e00f77423e34bed98b86e600def93393ba5822204fac57a29324db9f7a'
    }
    foreach ($path in $vendorHashes.Keys) {
        if (!(Test-Path -LiteralPath $path -PathType Leaf) -or
            (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $vendorHashes[$path]) {
            throw "Vendor file is missing or does not match the pinned SHA256: $path. Run build/fetch-vendor.ps1."
        }
    }

    Write-Host '== build with PyInstaller =='
    python -m PyInstaller --noconfirm --clean build/PhoneNAS.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    foreach ($privateFolder in @('data', 'baselines', 'logs')) {
        if (Test-Path (Join-Path dist/PhoneNAS $privateFolder)) { throw "Refusing to package private runtime state: $privateFolder" }
    }

    Write-Host '== prepare portable package =='
    Copy-Item build/portable.flag dist/PhoneNAS/portable.flag -Force
    Copy-Item README.md dist/PhoneNAS/README.md -Force
    Copy-Item RELEASE_NOTES.md dist/PhoneNAS/RELEASE_NOTES.md -Force
    Copy-Item build/LICENSES.md dist/PhoneNAS/LICENSES.md -Force
    Copy-Item LICENSE dist/PhoneNAS/LICENSE -Force
    $zip = "dist/PhoneNAS-portable-$version.zip"
    Compress-Archive -Path dist/PhoneNAS/* -DestinationPath $zip -Force
    Write-Host "portable zip: $zip"

    # The directory/installed copy uses APPDATA, not a bundled portable profile.
    Remove-Item dist/PhoneNAS/portable.flag -Force

    Write-Host '== build installer =='
    $isccCandidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
    )
    $iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($iscc) {
        & $iscc build/installer.iss
        if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
        $installer = "dist/PhoneNAS-Setup-$version.exe"
        Write-Host "installer: $installer"
        Get-FileHash $zip, $installer -Algorithm SHA256 | Format-Table -AutoSize
    } else {
        Write-Warning 'Inno Setup is not installed; only the portable ZIP was built.'
    }
} finally {
    Pop-Location
}
