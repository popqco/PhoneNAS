from __future__ import annotations

import json
import re
from pathlib import Path

from app.core.relay import BaselineStore, ContentSnapshot, _atomic_json, _no_links, _read_file


class PendingOperation:
    """A content-bound intent, never an automatic authorization to resume writes."""

    def __init__(self, store: BaselineStore):
        self.path = store.path.with_suffix(".pending.json")
        self.binding = store.binding

    def load(self) -> dict | None:
        try:
            value = json.loads(_read_file(self.path))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, UnicodeError) as exc:
            raise RuntimeError("未完成操作记录不可读；请先保留两端文件并人工核对。") from exc
        try:
            if not isinstance(value, dict) or value.get("version") != 1 or value.get("binding") != self.binding:
                raise ValueError("操作身份已变化")
            if value["operation"] not in ("publish", "receive", "return", "adopt"):
                raise ValueError("无效操作")
            for name in ("generation", "lock_token"):
                if not isinstance(value[name], str) or not re.fullmatch(r"[0-9a-f]{32}", value[name]):
                    raise ValueError("无效批次")
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{6}", value["stamp"]):
                raise ValueError("无效历史批次")
            value["local"] = ContentSnapshot.from_dict(value["local"])
            value["remote"] = ContentSnapshot.from_dict(value["remote"])
            previous = value.get("prior_metadata")
            if previous is not None:
                required = {"version", "dataset_id", "remote_path", "versions_path", "excludes",
                            "generation", "phase", "content_digest"}
                if (type(previous) is not dict or not required.issubset(previous)
                        or set(previous) - required - {"writer_identity"}
                        or type(previous["version"]) is not int or previous["version"] != 1
                        or previous["phase"] != "ready"
                        or any(previous[key] != self.binding[key] for key in
                               ("dataset_id", "remote_path", "versions_path", "excludes"))
                        or not re.fullmatch(r"[0-9a-f]{32}", previous["generation"])
                        or previous["content_digest"] != value["remote"].digest
                        or ("writer_identity" in previous and not re.fullmatch(
                            r"[0-9a-f]{32}", previous["writer_identity"]))):
                    raise ValueError("无效的操作前发布状态")
        except (KeyError, ValueError, TypeError) as exc:
            raise RuntimeError("未完成操作记录已损坏或身份变化，不能直接续作。") from exc
        return value

    def save(self, *, operation: str, local: ContentSnapshot, remote: ContentSnapshot,
             generation: str, stamp: str, lock_token: str, prior_metadata: dict | None = None) -> None:
        _atomic_json(self.path, {
            "version": 1, "binding": self.binding, "operation": operation,
            "local": local.to_dict(), "remote": remote.to_dict(),
            "generation": generation, "stamp": stamp, "lock_token": lock_token,
            "prior_metadata": prior_metadata,
        })

    def clear(self) -> None:
        _no_links(self.path)
        self.path.unlink(missing_ok=True)
