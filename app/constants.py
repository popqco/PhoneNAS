from __future__ import annotations

import sys
from pathlib import Path

APP_NAME = "PhoneNAS"
APP_VERSION = "1.2.6"

DEFAULT_URL = "http://100.109.166.11:8888"
DEFAULT_REMOTE = "phone"
DEFAULT_DRIVE = "X:"
DEFAULT_LOCAL_PATH = r"D:\LefeetHardware"
DEFAULT_INTERVAL_MINUTES = 20
DEFAULT_RETENTION_DAYS = 30

INTERVAL_CHOICES = [5, 10, 15, 20, 30, 60]
RETENTION_CHOICES = [7, 14, 30, 60, 90]

# Recommended exclusions: reproducible build/cache output, not user data.
# "Release/" is intentionally kept because it can hold deliverable APKs.
RECOMMENDED_EXCLUDES = [
    "node_modules/**",
    "**/node_modules/**",
    "app/build/**",
    "**/app/build/**",
    "build/**",
    "**/build/**",
    "obj/**",
    "**/obj/**",
    ".gradle/**",
    "**/.gradle/**",
    "__pycache__/**",
    "**/__pycache__/**",
    "*.tmp",
    "~$*",
    "Thumbs.db",
    "desktop.ini",
]

DRIVE_LETTERS = [f"{c}:" for c in "DEFGHIJKLMNOPQRSTUVWXYZ"]


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return base / relative
