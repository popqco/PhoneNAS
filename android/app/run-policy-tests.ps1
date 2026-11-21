# Public lifecycle/storage seam only. Does not invoke Gradle, Android, or a device.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$main = Join-Path $PSScriptRoot 'src/main/java/com/phonenas/server/probe/policy'
$test = Join-Path $PSScriptRoot 'src/test/java/com/phonenas/server/probe/policy'
$out = Join-Path $PSScriptRoot 'build/policy-contract'
New-Item -ItemType Directory -Force -Path $out | Out-Null
& javac --release 17 -encoding UTF-8 -Xlint:all -Werror -d $out `
    (Join-Path $main 'RuntimePolicy.java') `
    (Join-Path $main 'GuardedRuntimePolicy.java') `
    (Join-Path $test 'RuntimePolicyContractTest.java')
if ($LASTEXITCODE -ne 0) { throw 'Pure Java policy compilation failed.' }
& java -cp $out com.phonenas.server.probe.policy.RuntimePolicyContractTest
if ($LASTEXITCODE -ne 0) { throw 'Pure Java policy contract failed.' }
