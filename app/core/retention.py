from __future__ import annotations

import re
from datetime import datetime, timedelta

VERSION_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{6})$")


def parse_version_name(name: str) -> datetime | None:
    match = VERSION_RE.match(name.strip().strip("/"))
    if not match:
        return None
    try:
        return datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H%M%S")
    except ValueError:
        return None


def old_versions(names: list[str], retention_days: int, now: datetime | None = None) -> list[str]:
    """Return version directory names older than retention_days."""
    now = now or datetime.now()
    cutoff = now - timedelta(days=max(1, int(retention_days)))
    result = []
    for name in names:
        clean = name.strip().strip("/")
        stamp = parse_version_name(clean)
        if stamp is not None and stamp < cutoff:
            result.append(clean)
    return result


def version_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")
