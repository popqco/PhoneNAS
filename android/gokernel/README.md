# Native DAV kernel · confined Android file host

> The native kernel serves fixed-VPN HTTPS with a stable P-256 identity, remote revocation, dual-listener startup/stop, and public-root identity checks. See the [acceptance summary](../../docs/ANDROID-ACCEPTANCE.md) for later measured device results. The historical 0.2 results below are not current release evidence.

Public seams:

- `gokernel.New(Config) → Engine` implements authenticated `http.Handler`; call `Close` after the HTTP host has shut down.
- `mobile.Start(configJSON) → Server` owns an actual loopback HTTP listener and optional fixed `100.64/10` IPv4 HTTPS listener. `Address`, `Status`, `Certificate`, `RevokeRemote`, `Stop` are bound into the Android AAR; both listeners share one engine, limits and accounting.

The mature `golang.org/x/net/webdav v0.62.0` implementation supplies GET/HEAD/Range and finite-depth PROPFIND/XML behavior. A confined `os.Root` filesystem Adapter maps only named configured exports and a separate `.phonenas` control root. PUT/MKCOL/MOVE/DELETE are deliberately handled explicitly: stock file close/rename behavior cannot guarantee PhoneNAS's required interruption/exclusivity contracts.

## Contracts implemented and tested on the Windows host

- Every method authenticates; constant-time digest comparison, strong-password configuration minimum, no secret/path logging.
- Exact 201/405 MKCOL uses one exclusive mkdir. Dataset locks and owner files are persistent filesystem resources, not in-memory WebDAV LOCK state.
- PUT uses exclusive hidden same-root staging, bounded streaming, length/context validation, sync/close, target recheck and rename only after complete receipt. Interrupted new files are not exported; old complete targets remain readable.
- Same-export MOVE never deletes an existing destination before rename. Cross-export, foreign authority, invalid conditional/type/collection replacement requests fail explicitly. Full recursive COPY is not implemented (501); LOCK/UNLOCK are not advertised and return 405.
- Read-only writes rejected, synthetic root never lists outside configured exports, reserved staging/control aliases hidden, canonical non-overlapping nonsymlink roots required.
- Authenticated non-PUT bodies are actually bounded to 64 KiB before XML parsing, for known/chunked lengths and trailing data. PUT defaults to a 64 GiB cap (large limit not exercised at full size).
- Debug bridge is loopback-only with request/concurrency/time/header bounds. Stop drains then cancels active connections, waits for handlers, closes descriptors; it does not clear locks or delete user data.

## Validation

From the module directory with Go 1.27.4:

```text
go test ./... -count=1 -v
go vet ./...
```

Current executed total: **15 tests, 14 passed, 1 skipped** (Windows symlink privilege). Root suite includes 12 tests, mobile 3. Metadata oversize and canceled MKCOL were recorded RED→GREEN. A real TCP slow PUT plus queued MKCOL test confirms `Server.Stop()` does not publish the partial upload or create a canceled collection.

These Go host tests use disposable local directories and listeners. They never read PhoneNAS's existing configuration or contact the phone. Native binding compilation is performed separately by `../build-native.ps1` and requires the pinned gomobile/NDK.

On 2026-11-15, an authorized ordinary USB install succeeded on vivo V2227A / Android 16. `../davcore/test_native_contract.py` completed **8/8 checks twice** against the real native listener, with MainActivity foreground and only App-owned demo roots exported. This covers exclusive mkdir, interrupted PUT integrity, rename/MOVE, Range, encoded names and read-only behavior. A separate real UI Stop during a slow PUT and queued MKCOL confirmed cancellation, complete old-file preservation and persistent lock/owner state across UI restart. Evidence lives in `../validation/*device-20261115*.json`.

## Limits, not hidden passes

- Real-device foreground file contracts passed; **background availability failed** under default vivo policy. Home left the specialUse FGS alive but the OEM process state reported frozen; HTTP stopped responding from both USB and phone-local probes until MainActivity returned, with the same PID. No freezer bypass, PC watchdog or permission-setting changes were made.
- `?` filenames remain unsupported by the Windows test filesystem, but Chinese, spaces and `%/#/?` roundtrips passed on the real Android demo filesystem. Arbitrary user-selected public storage is not covered by App-specific demo tests.
- Race-detector run was unavailable because the host Go environment lacks cgo C compiler support; not reported as passing.
- Root confinement is not a directory snapshot or a transaction against arbitrary external apps editing within a shared root. Local filesystem writers remain trusted. No content cache or hash extension exists yet.
- POSIX rename durability after power loss, arbitrary recursive directory transactions and multi-user ACLs are not claimed. Typed collection replacement is refused rather than destructively emulated.
- Coarse serialization favors simple correctness over peak performance in this debug build; there is no measured speed improvement claim.

See the [Android build guide](../README.md) for the toolchain. Historical raw device reports stay local. Upstream license text is included in the Android APK's `assets/OPEN_SOURCE_NOTICES.txt`.
