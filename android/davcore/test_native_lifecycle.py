"""Finite lifecycle checks for the authorized, isolated Android debug package.

Requires an already running demo listener and its private debug credentials.
Never reboots a phone, changes networking/settings, or schedules a watchdog.
Use --scenario background for a genuinely request-free idle interval.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import runpy
import socket
import subprocess
import time

PACKAGE = "com.phonenas.server.probe"
DEMO_ROOT = "/storage/emulated/0/Android/data/" + PACKAGE + "/files/contract-test"


class Device:
    def __init__(self, adb: Path, serial: str):
        self.base = [str(adb), "-s", serial]

    def call(self, *args, check=True, timeout=20):
        return subprocess.run(self.base + list(args), capture_output=True,
                              check=check, timeout=timeout)

    def shell(self, *args, check=True):
        return self.call("shell", *args, check=check).stdout.decode("utf-8", errors="replace")

    def service(self):
        return "ServiceRecord{" in self.shell("dumpsys", "activity", "services", PACKAGE)

    def open(self):
        self.shell("am", "start", "-W", "-n", PACKAGE + "/.MainActivity")

    def control(self, action):
        self.shell("am", "start", "-W", "-n", PACKAGE + "/.DebugControlActivity",
                   "-a", PACKAGE + ".DEBUG_" + action + "_DEMO")


def probe(client, path="/test/readme.txt", *, authenticated=True):
    connection = http.client.HTTPConnection("127.0.0.1", client.port, timeout=4)
    started = time.monotonic()
    try:
        headers = {"Authorization": client.authorization} if authenticated else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read(4097)
        return {"http": response.status,
                "fixture": body == b"PhoneNAS isolated test demo. No personal folders are exported.\n",
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1)}
    except (OSError, http.client.HTTPException) as error:
        return {"transport": type(error).__name__,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1)}
    finally:
        connection.close()


def background(device, client, result, idle_seconds):
    result["pid_before"] = device.shell("pidof", PACKAGE).strip()
    device.shell("input", "keyevent", "KEYCODE_HOME")
    # No HTTP, UI automation, or ADB process polling during this idle interval.
    time.sleep(idle_seconds)
    result["request_free_idle_seconds"] = idle_seconds
    result["after_idle"] = probe(client)
    services = device.shell("dumpsys", "activity", "services", PACKAGE)
    processes = device.shell("dumpsys", "activity", "processes", PACKAGE)
    power = device.shell("dumpsys", "power")
    result["foreground_service_lines"] = [line.strip() for line in services.splitlines()
                                          if "isForeground=" in line]
    result["freeze_lines"] = [line.strip() for line in processes.splitlines()
                               if "isFrozen=" in line or "allowFreeze=" in line]
    result["power_lines"] = [line.strip() for line in power.splitlines()
                              if any(key in line for key in ("mWakefulness=", "mIsPowered=",
                                                             "mDeviceIdleMode=", "mLightDeviceIdleMode="))]
    result["pid_after_idle"] = device.shell("pidof", PACKAGE).strip()
    device.open()
    result["after_foreground_return"] = probe(client)
    result["same_pid"] = result["pid_before"] == result["pid_after_idle"]
    assert result["after_idle"].get("http") == 200 and result["after_idle"].get("fixture"), \
        "No valid demo read after request-free background idle"


def force_stop(device, client, result, _idle_seconds):
    device.shell("am", "force-stop", PACKAGE)
    time.sleep(5)
    result["while_stopped"] = {
        "pid": device.shell("pidof", PACKAGE, check=False).strip(),
        "service": device.service(),
        "listener": probe(client, authenticated=False),
    }
    assert not result["while_stopped"]["pid"] and not result["while_stopped"]["service"]
    assert "http" not in result["while_stopped"]["listener"]
    device.open()
    # More than the complete 2/5/15 second bounded recovery delays.
    time.sleep(18)
    result["after_launcher_only"] = {"service": device.service(),
                                      "listener": probe(client, authenticated=False)}
    assert not result["after_launcher_only"]["service"], "Opening launcher started a service"
    assert "http" not in result["after_launcher_only"]["listener"], "Opening launcher opened a listener"
    device.control("START")
    result["after_explicit_start"] = wait_for_fixture(client)
    assert result["after_explicit_start"].get("http") == 200


def wait_for_fixture(client):
    for _ in range(20):
        sample = probe(client)
        if sample.get("http") == 200 and sample.get("fixture"):
            return sample
        time.sleep(0.3)
    raise AssertionError("Explicit demo start did not produce a valid fixture")


def stop_restart(device, client, result, _idle_seconds):
    client.prepare()
    owner = b'{"token":"isolated-finite-lifecycle-proof"}'
    for parent in ("/.phonenas/", "/.phonenas/locks/"):
        assert client.request("MKCOL", parent)[0] in (201, 405)
    assert client.request("MKCOL", client.lock + "/")[0] == 201
    client.owns_lock = True
    assert client.request("PUT", client.lock + "/owner.json", owner)[0] == 201
    result["scratch"] = client.prefix
    result["lock"] = client.lock
    path = client.file("complete-before-stop.bin")
    old = b"complete-before-finite-stop-test"
    assert client.request("PUT", path, old)[0] == 201
    slow = socket.create_connection(("127.0.0.1", client.port), timeout=4)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        slow.sendall((f"PUT {path} HTTP/1.1\r\nHost: 127.0.0.1:{client.port}\r\n"
                      f"Authorization: {client.authorization}\r\nContent-Length: 1048576\r\n"
                      "Connection: close\r\n\r\n").encode("ascii") + b"unfinished")
        physical = "/sdcard/Android/data/" + PACKAGE + "/files/contract-test" + client.prefix
        for _ in range(20):
            if ".phonenas-temp-" in device.shell("ls", "-a", physical):
                break
            time.sleep(0.1)
        else:
            raise AssertionError("Active partial staging not observed")
        def queued_mkcol():
            try:
                return {"http": client.request("MKCOL", client.prefix + "/must-not-appear/")[0]}
            except (OSError, http.client.HTTPException) as error:
                return {"transport": type(error).__name__}

        queued = pool.submit(queued_mkcol)
        time.sleep(0.4)
        result["mkcol_pending_before_stop"] = not queued.done()
        assert result["mkcol_pending_before_stop"], "MKCOL did not wait behind active PUT"
        started = time.monotonic()
        device.control("STOP")
        result["queued_mkcol_result"] = queued.result(timeout=12)
        assert not 200 <= result["queued_mkcol_result"].get("http", 0) < 300
        for _ in range(30):
            if not device.service():
                break
            time.sleep(0.3)
        else:
            raise AssertionError("Service did not confirm Stop")
        result["stop_seconds"] = round(time.monotonic() - started, 3)
    finally:
        slow.close()
        pool.shutdown(wait=True)
    device.control("START")
    wait_for_fixture(client)
    status, _, contents = client.request("GET", path)
    result["old_file_intact"] = status == 200 and contents == old
    result["no_partial_staging"] = ".phonenas-temp-" not in device.shell("ls", "-a", physical)
    result["existing_lock_http"] = client.request("MKCOL", client.lock + "/")[0]
    result["owner_intact"] = client.request("GET", client.lock + "/owner.json")[2] == owner
    result["cancelled_mkcol_absent_http"] = client.request("GET", client.prefix + "/must-not-appear/")[0]
    assert result["cancelled_mkcol_absent_http"] == 404
    assert result["old_file_intact"] and result["no_partial_staging"]
    assert result["existing_lock_http"] == 405 and result["owner_intact"]
    # Release only the lock whose exact owner this run just read and verified.
    result["lock_cleanup_http"] = client.request("DELETE", client.lock + "/")[0]
    client.owns_lock = False
    result["scratch_cleanup_http"] = client.request("DELETE", client.prefix + "/")[0]
    client.owns_tree = False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--scenario", choices=("background", "force-stop", "stop-restart"), required=True)
    parser.add_argument("--idle-seconds", type=int, default=30)
    args = parser.parse_args()
    if not 5 <= args.idle_seconds <= 120:
        parser.error("Idle interval must be between 5 and 120 seconds")
    credentials = json.loads(args.credentials.read_text("utf-8"))
    if credentials.get("root") != DEMO_ROOT:
        parser.error("Only the fixed App-owned demo root is accepted")
    namespace = runpy.run_path(str(Path(__file__).with_name("test_native_contract.py")))
    client = namespace["Client"](args.url, credentials["username"], credentials["password"])
    device = Device(args.adb, args.serial)
    result = {"scope": "Finite isolated Android demo lifecycle test, not PC monitoring",
              "scenario": args.scenario, "passed": False}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    # Reserve exclusive output before any phone or HTTP operation.
    with args.report.open("x", encoding="utf-8") as report:
        try:
            assert device.shell("getprop", "ro.product.model").strip() == "V2227A", \
                "This acceptance run is authorized only for the current vivo V2227A"
            result["precondition"] = probe(client)
            assert result["precondition"].get("http") == 200 and result["precondition"].get("fixture"), \
                "An already-running authenticated demo listener is required"
            {"background": background, "force-stop": force_stop,
             "stop-restart": stop_restart}[args.scenario](device, client, result, args.idle_seconds)
            result["passed"] = True
        except (AssertionError, OSError, http.client.HTTPException, subprocess.SubprocessError) as error:
            result["failure"] = type(error).__name__ + ": " + str(error)
        finally:
            if args.scenario != "background":
                try:
                    device.control("STOP")
                    time.sleep(1)
                    result["final_service_absent"] = not device.service()
                    if not result["final_service_absent"]:
                        result["passed"] = False
                except subprocess.SubprocessError as error:
                    result["stop_unconfirmed"] = type(error).__name__
                    result["passed"] = False
            report.seek(0)
            json.dump(result, report, ensure_ascii=False, indent=2)
            report.write("\n")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
