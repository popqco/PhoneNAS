param(
    [Parameter(Mandatory = $true)][string]$GoExe,
    [Parameter(Mandatory = $true)][string]$GoMobileExe,
    [string]$SdkRoot = 'D:\Android_SDK',
    [string]$NdkVersion = '27.2.12479018'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$env:ANDROID_HOME = $SdkRoot
$env:ANDROID_SDK_ROOT = $SdkRoot
$env:ANDROID_NDK_HOME = Join-Path $SdkRoot "ndk/$NdkVersion"
$env:GOTOOLCHAIN = 'local'
$env:PATH = (Split-Path $GoExe) + ';' + (Split-Path $GoMobileExe) + ';' + $env:PATH
if (-not (Test-Path (Join-Path $env:ANDROID_NDK_HOME 'source.properties'))) {
    throw 'The selected NDK is not installed. This script never downloads tools.'
}
$module = Join-Path $PSScriptRoot 'gokernel'
$out = Join-Path $PSScriptRoot 'app/libs'
New-Item -ItemType Directory -Force -Path $out | Out-Null
Push-Location $module
try {
    & $GoExe test ./...
    if ($LASTEXITCODE -ne 0) { throw 'Native HTTP contracts failed; refusing to bind.' }
    & $GoMobileExe bind -target=android/arm64 -androidapi=30 '-ldflags=-extldflags=-Wl,-z,max-page-size=16384' -o (Join-Path $out 'phonenas-kernel.aar') ./mobile
    if ($LASTEXITCODE -ne 0) { throw 'Android native library binding failed.' }
} finally {
    Pop-Location
}
