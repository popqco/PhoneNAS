from __future__ import annotations

import sys
from pathlib import Path

APP_NAME = "PhoneNAS"
APP_VERSION = "1.2.8"

# Old profile fallback pairs. An omitted field alone does not identify the release.
LEGACY_DEFAULT_URL = "http://100.109.166.11:8888"
LEGACY_DEFAULT_USERNAME = "admin"
PREVIEW_DEFAULT_URL = "https://100.109.166.11:18889"  # 58e0eac
PREVIEW_DEFAULT_USERNAME = "phonenas"
DEFAULT_LOCAL_PATH = r"D:\LefeetHardware"

DEFAULT_REMOTE = "phone"
DEFAULT_DRIVE = "X:"
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
