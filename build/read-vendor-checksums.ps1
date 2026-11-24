$hashes = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'vendor-checksums.json') -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
foreach ($name in @('rclone.exe', 'winfsp.msi')) {
    $hash = $hashes.$name
    if ($hash -isnot [string] -or $hash -notmatch '\A[0-9a-f]{64}\z') {
        throw "Missing or invalid pinned SHA256 for $name in build/vendor-checksums.json."
    }
}
$hashes
