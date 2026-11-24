param(
    [string]$Mirror = ''
)

$ErrorActionPreference = 'Stop'

$vendorHashes = & (Join-Path $PSScriptRoot 'read-vendor-checksums.ps1')

$root = Split-Path $PSScriptRoot -Parent
$vendor = Join-Path $root 'vendor'
New-Item -ItemType Directory -Force -Path $vendor | Out-Null

$rcloneVersion = '1.76.0'
$rcloneSha256 = $vendorHashes.'rclone.exe'
$winfspVersion = '2.1.25156'
$winfspSha256 = $vendorHashes.'winfsp.msi'

Write-Host "== rclone v$rcloneVersion =="
$rcloneZip = Join-Path $env:TEMP "rclone-v$rcloneVersion-windows-amd64.zip"
$rcloneUrl = "https://downloads.rclone.org/v$rcloneVersion/rclone-v$rcloneVersion-windows-amd64.zip"
Invoke-WebRequest -Uri $rcloneUrl -OutFile $rcloneZip
$rcloneDir = Join-Path $env:TEMP "rclone-v$rcloneVersion-windows-amd64"
Expand-Archive -Path $rcloneZip -DestinationPath $env:TEMP -Force
Copy-Item (Join-Path $rcloneDir 'rclone.exe') (Join-Path $vendor 'rclone.exe') -Force

$hash = (Get-FileHash (Join-Path $vendor 'rclone.exe') -Algorithm SHA256).Hash.ToLower()
if ($hash -ne $rcloneSha256) {
    throw "rclone SHA256 mismatch: $hash"
}
Write-Host "rclone.exe ok ($hash)"

Write-Host "== WinFsp $winfspVersion =="
$msi = Join-Path $vendor 'winfsp.msi'
$winfspPath = "winfsp/winfsp/releases/download/v2.1/winfsp-$winfspVersion.msi"
$winfspUrl = if ($Mirror) { "$($Mirror.TrimEnd('/'))/https://github.com/$winfspPath" } else { "https://github.com/$winfspPath" }
Invoke-WebRequest -Uri $winfspUrl -OutFile $msi

$hash = (Get-FileHash $msi -Algorithm SHA256).Hash.ToLower()
if ($hash -ne $winfspSha256) {
    throw "WinFsp MSI SHA256 mismatch: $hash"
}
Write-Host "winfsp.msi ok ($hash)"

Write-Host 'vendor files ready.'
