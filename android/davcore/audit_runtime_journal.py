#!/usr/bin/env python3
"""Independent auditor for a PhoneNAS runtime-journal export.

Reads the ASCII text produced by the app's "export journal" (system
CreateDocument) and verifies structure, CRC, run identity, generation and
gap semantics. It does not connect to the device and never claims uptime:
`continuousClaim` is false by design in the producer, and this tool reports
only what the exported records actually contain.

Usage:
    python audit_runtime_journal.py <export.txt> [--json-out report.json]

Exit code 0 means the export is structurally valid. Threshold observations
(temperature, gaps, restarts) are reported as data, not silently converted
into a pass.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

HEADER_NOTE = "PhoneNAS local runtime record; continuousClaim=false; not an acceptance test."
HEADER_COLUMNS = (
    "schema|runUUID|buildVersion|sequence|utcMillis|elapsedRealtimeMillis|bootCount|"
    "processGeneration|engineGeneration|event|running|desired|recoveries|requests|"
    "receivedBytes|sentBytes|screenInteractive|charging|batteryPercent|"
    "batteryTemperatureTenthsC|failureCode|gap|crc32"
)
SCHEMA = 1
MAX_OBSERVATION_GAP_MILLIS = 20 * 60 * 1000
MAX_LINE = 1024
EVENTS = {
    "START", "READY", "CHECKPOINT", "STOP_REQUESTED", "STOP_CONFIRMED",
    "FAILURE", "PROCESS_START", "RECOVERY",
}
FAILURES = {
    "UNKNOWN", "ENGINE_FAILURE", "START_REJECTED", "STOP_UNCONFIRMED",
    "RECOVERY_EXHAUSTED", "STORAGE_UNAVAILABLE",
}
STOP_EVENTS = {"STOP_REQUESTED", "STOP_CONFIRMED"}


class Invalid(Exception):
    pass


def _int(text: str, field: str) -> int:
    if not text or text.lstrip("-").isdigit() is False:
        raise Invalid(f"{field} is not an integer: {text!r}")
    value = int(text)
    if str(value) != text:
        raise Invalid(f"{field} is not canonical: {text!r}")
    return value


def _optional_int(text: str, field: str) -> int | None:
    return None if text == "-" else _int(text, field)


def _bool(text: str, field: str) -> bool:
    if text == "1":
        return True
    if text == "0":
        return False
    raise Invalid(f"{field} is not a bit: {text!r}")


def _optional_bool(text: str, field: str) -> bool | None:
    return None if text == "-" else _bool(text, field)


def _uuid(text: str, field: str) -> str:
    parsed = uuid.UUID(text)
    if str(parsed) != text:
        raise Invalid(f"{field} is not canonical: {text!r}")
    return text


def _crc(payload: str) -> str:
    return format(zlib.crc32(payload.encode("ascii")) & 0xFFFFFFFF, "08x")


def parse_line(line: str, number: int) -> dict:
    if len(line) > MAX_LINE:
        raise Invalid(f"line {number} exceeds {MAX_LINE} bytes")
    fields = line.split("|")
    if len(fields) != 23:
        raise Invalid(f"line {number} has {len(fields)} fields, expected 23")
    if fields[0] != str(SCHEMA):
        raise Invalid(f"line {number} schema {fields[0]!r}")
    if _crc("|".join(fields[:22])) != fields[22]:
        raise Invalid(f"line {number} CRC mismatch")

    entry = {
        "line": number,
        "run": _uuid(fields[1], "runUUID"),
        "build": _int(fields[2], "buildVersion"),
        "sequence": _int(fields[3], "sequence"),
        "utc": _int(fields[4], "utcMillis"),
        "elapsed": _int(fields[5], "elapsedRealtimeMillis"),
        "boot": _optional_int(fields[6], "bootCount"),
        "process": _uuid(fields[7], "processGeneration"),
        "engine": _optional_int(fields[8], "engineGeneration"),
        "event": fields[9],
        "running": _bool(fields[10], "running"),
        "desired": _bool(fields[11], "desired"),
        "recoveries": _int(fields[12], "recoveries"),
        "requests": _int(fields[13], "requests"),
        "received": _int(fields[14], "receivedBytes"),
        "sent": _int(fields[15], "sentBytes"),
        "screen": _optional_bool(fields[16], "screenInteractive"),
        "charging": _optional_bool(fields[17], "charging"),
        "battery": _optional_int(fields[18], "batteryPercent"),
        "temperature": _optional_int(fields[19], "batteryTemperatureTenthsC"),
        "failure": None if fields[20] == "-" else fields[20],
        "gap": _bool(fields[21], "gap"),
    }

    if entry["build"] < 0:
        raise Invalid(f"line {number} negative build")
    if entry["utc"] not in range(0, 253_402_300_799_999 + 1):
        raise Invalid(f"line {number} utc out of range")
    if entry["elapsed"] < 0:
        raise Invalid(f"line {number} negative elapsed")
    if entry["boot"] is not None and entry["boot"] < 0:
        raise Invalid(f"line {number} negative bootCount")
    if entry["engine"] is not None and entry["engine"] < 0:
        raise Invalid(f"line {number} negative engineGeneration")
    if min(entry["recoveries"], entry["requests"], entry["received"], entry["sent"]) < 0:
        raise Invalid(f"line {number} negative counter")
    if entry["battery"] is not None and not 0 <= entry["battery"] <= 100:
        raise Invalid(f"line {number} batteryPercent out of range")
    if entry["temperature"] is not None and not -1_000 <= entry["temperature"] <= 1_500:
        raise Invalid(f"line {number} temperature out of range")
    if entry["event"] not in EVENTS:
        raise Invalid(f"line {number} event {entry['event']!r}")
    if entry["failure"] is not None and entry["failure"] not in FAILURES:
        raise Invalid(f"line {number} failureCode {entry['failure']!r}")
    if (entry["event"] == "FAILURE") != (entry["failure"] is not None):
        raise Invalid(f"line {number} FAILURE/failureCode mismatch")
    if entry["event"] == "READY" and not entry["running"]:
        raise Invalid(f"line {number} READY while not running")
    if entry["event"] == "STOP_CONFIRMED" and entry["running"]:
        raise Invalid(f"line {number} STOP_CONFIRMED while running")
    return entry


def raw_gap(before: dict, current: dict) -> bool:
    return (
        before["process"] != current["process"]
        or before["boot"] != current["boot"]
        or current["elapsed"] < before["elapsed"]
        or current["utc"] < before["utc"]
        or current["elapsed"] - before["elapsed"] > MAX_OBSERVATION_GAP_MILLIS
        or current["utc"] - before["utc"] > MAX_OBSERVATION_GAP_MILLIS
    )


def expected_gap(before: dict | None, current: dict) -> bool:
    if before is None:
        return current["event"] in {"START"}
    return (
        raw_gap(before, current)
        or current["event"] in {"PROCESS_START", "RECOVERY"}
        or (
            before["running"]
            and not current["running"]
            and current["event"] not in STOP_EVENTS
        )
        or (
            before["engine"] is not None
            and current["engine"] is not None
            and before["engine"] != current["engine"]
        )
    )


def parse_export(text: str) -> list[dict]:
    lines = text.split("\n")
    if len(lines) >= 1 and lines[-1] == "":
        lines.pop()
    if len(lines) < 3:
        raise Invalid("export is shorter than one record")
    if lines[0] != HEADER_NOTE:
        raise Invalid("unexpected note header")
    if lines[1] != HEADER_COLUMNS:
        raise Invalid("unexpected column header")
    entries = [parse_line(line, index + 3) for index, line in enumerate(lines[2:])]
    validate_runs(entries)
    return entries


def validate_runs(entries: list[dict]) -> None:
    runs: list[list[dict]] = []
    for entry in entries:
        if not runs or entry["run"] != runs[-1][0]["run"]:
            runs.append([entry])
        else:
            runs[-1].append(entry)

    for run in runs:
        first = run[0]
        if first["event"] != "START" or first["running"]:
            raise Invalid(f"run {first['run']} does not start with a stopped START")
        if first["sequence"] != 1:
            raise Invalid(f"run {first['run']} sequence does not start at 1")
        for number, current in enumerate(run):
            if current["sequence"] != number + 1:
                raise Invalid(f"run {current['run']} sequence {current['sequence']} != {number + 1}")
            if number > 0 and current["event"] == "START":
                raise Invalid(f"run {current['run']} repeats START")
            if current["event"] == "STOP_CONFIRMED" and number != len(run) - 1:
                raise Invalid(f"run {current['run']} STOP_CONFIRMED is not last")
        for before, current in zip(run, run[1:]):
            if before["process"] != current["process"] and current["event"] != "PROCESS_START":
                raise Invalid(f"run {current['run']} process changed without PROCESS_START")
            if raw_gap(before, current) and not current["gap"]:
                raise Invalid(f"run {current['run']} unmarked raw gap at sequence {current['sequence']}")
            if current["gap"] and not expected_gap(before, current):
                # Kotlin marks more cases than the minimum; keep this visible but not fatal.
                current.setdefault("notes", []).append("gap flag has no detected cause")
    if runs:
        if any(run[-1]["event"] != "STOP_CONFIRMED" for run in runs[:-1]):
            raise Invalid("an earlier run does not end with STOP_CONFIRMED")
        if len({run[0]["run"] for run in runs}) != len(runs):
            raise Invalid("duplicate run UUID across segments")


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def summarize_run(run: list[dict]) -> dict:
    observed = 0
    for before, after in zip(run, run[1:]):
        if (
            not after["gap"]
            and before["process"] == after["process"]
            and before["boot"] == after["boot"]
            and before["running"]
            and after["running"]
            and before["engine"] == after["engine"]
        ):
            observed += after["elapsed"] - before["elapsed"]
    max_delta = max(
        (after["utc"] - before["utc"]
         for before, after in zip(run, run[1:]) if before["boot"] == after["boot"]),
        default=0,
    )
    temperatures = [entry["temperature"] for entry in run if entry["temperature"] is not None]
    consecutive_hot = 0
    previous_hot = False
    for value in temperatures:
        hot = value >= 450
        if hot and previous_hot:
            consecutive_hot += 1
        previous_hot = hot
    return {
        "run_id": run[0]["run"],
        "records": len(run),
        "first_utc": iso(run[0]["utc"]),
        "last_utc": iso(run[-1]["utc"]),
        "span_hours": round((run[-1]["utc"] - run[0]["utc"]) / 3_600_000, 3),
        "first_event": run[0]["event"],
        "last_event": run[-1]["event"],
        "active_at_export": run[-1]["event"] != "STOP_CONFIRMED",
        "build_versions": sorted({entry["build"] for entry in run}),
        "process_generations": sorted({entry["process"] for entry in run}),
        "engine_generations": sorted({
            entry["engine"] for entry in run if entry["engine"] is not None
        }),
        "boot_counts": sorted({
            entry["boot"] for entry in run if entry["boot"] is not None
        }),
        "observed_running_elapsed_hours": round(observed / 3_600_000, 3),
        "max_same_boot_record_gap_minutes": round(max_delta / 60_000, 3),
        "gap_flag_count": sum(1 for entry in run if entry["gap"]),
        "gap_sequences": [entry["sequence"] for entry in run if entry["gap"]],
        "failure_count": sum(1 for entry in run if entry["event"] == "FAILURE"),
        "failure_codes": [entry["failure"] for entry in run if entry["event"] == "FAILURE"],
        "stop_events": [entry["event"] for entry in run if entry["event"] in STOP_EVENTS],
        "temperature_tenths_c_max": max(temperatures, default=None),
        "temperature_consecutive_45c_pairs": consecutive_hot,
        "temperature_at_or_above_50c_count": sum(1 for value in temperatures if value >= 500),
    }


def audit(entries: list[dict]) -> dict:
    runs: list[list[dict]] = []
    for entry in entries:
        if not runs or entry["run"] != runs[-1][0]["run"]:
            runs.append([entry])
        else:
            runs[-1].append(entry)
    run_summaries = [summarize_run(run) for run in runs]

    observed = 0
    for before, after in zip(entries, entries[1:]):
        if (
            not after["gap"]
            and before["process"] == after["process"]
            and before["boot"] == after["boot"]
            and before["running"]
            and after["running"]
            and before["engine"] == after["engine"]
        ):
            observed += after["elapsed"] - before["elapsed"]

    gaps = [
        {
            "sequence": entry["sequence"],
            "utc": iso(entry["utc"]),
            "event": entry["event"],
            "reason": "process" if entry["event"] == "PROCESS_START" else (
                "engine" if entry["event"] == "RECOVERY" else "raw/other"
            ),
        }
        for entry in entries
        if entry["gap"]
    ]

    max_delta = 0
    max_delta_pair = None
    for before, after in zip(entries, entries[1:]):
        if before["run"] == after["run"] and before["boot"] == after["boot"]:
            delta = after["utc"] - before["utc"]
            if delta > max_delta:
                max_delta = delta
                max_delta_pair = (before["sequence"], after["sequence"])

    temperatures = [
        entry["temperature"] for entry in entries if entry["temperature"] is not None
    ]
    consecutive_hot = 0
    previous_hot = False
    for value in temperatures:
        hot = value >= 450
        if hot and previous_hot:
            consecutive_hot += 1
        previous_hot = hot

    process_restarts = sum(1 for entry in entries if entry["event"] == "PROCESS_START")
    engine_generations = sorted({
        entry["engine"] for entry in entries if entry["engine"] is not None
    })
    process_generations = sorted({entry["process"] for entry in entries})
    boots = sorted({entry["boot"] for entry in entries if entry["boot"] is not None})
    failures = [
        {"sequence": entry["sequence"], "utc": iso(entry["utc"]), "code": entry["failure"]}
        for entry in entries
        if entry["event"] == "FAILURE"
    ]
    stops = [
        {
            "event": entry["event"],
            "sequence": entry["sequence"],
            "utc": iso(entry["utc"]),
            "requests": entry["requests"],
            "sent": entry["sent"],
            "received": entry["received"],
        }
        for entry in entries
        if entry["event"] in STOP_EVENTS
    ]
    last = entries[-1]
    first = entries[0]
    return {
        "scope": "Independent structural audit of exported local records; not an uptime or remote-availability claim.",
        "structural_valid": True,
        "continuous_claim": False,
        "records": len(entries),
        "runs": len(runs),
        "run_ids": [run[0]["run"] for run in runs],
        "run_summaries": run_summaries,
        "last_run": run_summaries[-1] if run_summaries else None,
        "build_versions": sorted({entry["build"] for entry in entries}),
        "process_generations": process_generations,
        "process_restart_count": process_restarts,
        "engine_generations": engine_generations,
        "boot_counts": boots,
        "boot_change_count": max(0, len(boots) - 1),
        "records_span_utc_hours": round((last["utc"] - first["utc"]) / 3_600_000, 3),
        "last_record_utc": iso(last["utc"]),
        "first_record_utc": iso(first["utc"]),
        "observed_running_elapsed_hours": round(observed / 3_600_000, 3),
        "max_same_boot_record_gap_minutes": round(max_delta / 60_000, 3),
        "max_gap_pair_sequences": max_delta_pair,
        "gap_flag_count": len(gaps),
        "gaps": gaps,
        "failure_count": len(failures),
        "failures": failures,
        "stop_records": stops,
        "active_at_export": last["event"] != "STOP_CONFIRMED",
        "final_requests": last["requests"],
        "final_received_bytes": last["received"],
        "final_sent_bytes": last["sent"],
        "charging_values": sorted({entry["charging"] for entry in entries}, key=str),
        "battery_percent_min": min(
            (entry["battery"] for entry in entries if entry["battery"] is not None),
            default=None,
        ),
        "battery_percent_max": max(
            (entry["battery"] for entry in entries if entry["battery"] is not None),
            default=None,
        ),
        "temperature_tenths_c_min": min(temperatures, default=None),
        "temperature_tenths_c_max": max(temperatures, default=None),
        "temperature_at_or_above_45c_count": sum(1 for value in temperatures if value >= 450),
        "temperature_consecutive_45c_pairs": consecutive_hot,
        "temperature_at_or_above_50c_count": sum(1 for value in temperatures if value >= 500),
        "thresholds": {
            "any_gap_flag": bool(gaps),
            "two_consecutive_45c_or_any_50c": consecutive_hot >= 1 or any(
                value >= 500 for value in temperatures
            ),
            "not_charging_observed": any(entry["charging"] is False for entry in entries),
            "multiple_boots": len(boots) > 1,
            "multiple_engines": len(engine_generations) > 1,
            "multiple_processes": len(process_generations) > 1,
        },
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)
    raw = args.export.read_bytes()
    if b"\x00" in raw or any(byte not in (10, 13, *range(32, 127)) for byte in raw):
        print("journal export contains non-ASCII or control bytes", file=sys.stderr)
        return 2
    text = raw.decode("ascii").replace("\r\n", "\n")
    try:
        entries = parse_export(text)
    except Invalid as error:
        print(f"invalid journal export: {error}", file=sys.stderr)
        return 1
    report = audit(entries)
    encoded = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.json_out:
        args.json_out.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
