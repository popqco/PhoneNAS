from __future__ import annotations

import re

from app.core.filenames import LEGACY, LITERAL
from app.core.relay import BaselineStore, ContentSnapshot, _atomic_json, _json, _no_links, _read_file


CONTROL_OPERATIONS = {
    "finalize": "已校验同步的收尾",
    "initialize_baseline": "共同基线初始化",
    "history_delete": "手机历史清理",
    "history_restore": "手机历史恢复",
    "rejected": "写入前停止的操作",
}


class PendingOperation:
    """A content-bound intent, never an automatic authorization to resume writes."""

    def __init__(self, store: BaselineStore):
        self.path = store.path.with_suffix(".pending.json")
        self.binding = store.binding

    def load(self) -> dict | None:
        try:
            value = _json(_read_file(self.path))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, UnicodeError, RuntimeError) as exc:
            raise RuntimeError("未完成操作记录不可读；请先保留两端文件并人工核对。") from exc
        try:
            if (type(value) is not dict or type(value.get("version")) is not int
                    or value["version"] not in (LEGACY, LITERAL) or value.get("binding") != self.binding):
                raise ValueError("操作身份已变化")
            if not isinstance(value["lock_token"], str) or not re.fullmatch(r"[0-9a-f]{32}", value["lock_token"]):
                raise ValueError("无效的锁凭据")
            if value["operation"] in CONTROL_OPERATIONS:
                if (value["version"] != 1
                        or set(value) != {"version", "binding", "operation", "lock_token", "registration"}
                        or type(value["registration"]) is not bool
                        or (value["registration"] and value["operation"] not in (
                            "initialize_baseline", "history_delete", "finalize"))):
                    raise ValueError("无效的锁恢复记录")
                return value
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
                        or type(previous["version"]) is not int or previous["version"] != value["version"]
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
             generation: str, stamp: str, lock_token: str, prior_metadata: dict | None = None,
             protocol: int = LEGACY) -> None:
        if type(protocol) is not int or protocol not in (LEGACY, LITERAL):
            raise ValueError("无效的文件名协议")
        if prior_metadata is not None and prior_metadata.get("version") != protocol:
            raise ValueError("未完成操作的文件名协议与发布状态不一致")
        _atomic_json(self.path, {
            "version": protocol, "binding": self.binding, "operation": operation,
            "local": local.to_dict(), "remote": remote.to_dict(),
            "generation": generation, "stamp": stamp, "lock_token": lock_token,
            "prior_metadata": prior_metadata,
        })

    def save_control(self, operation: str, lock_token: str, *, registration: bool = False) -> None:
        if operation not in CONTROL_OPERATIONS or not re.fullmatch(r"[0-9a-f]{32}", lock_token):
            raise ValueError("无效的锁恢复记录")
        # Older clients must reject this intent, not mistake it for authorization
        # to replay a transfer. Filename protocol only applies to payload intents.
        _atomic_json(self.path, {"version": 1, "binding": self.binding, "operation": operation,
                                 "lock_token": lock_token, "registration": registration})

    def clear_control(self, token: str) -> None:
        value = self.load()
        if value is not None and value["operation"] in CONTROL_OPERATIONS and value["lock_token"] == token:
            self.clear()

    def clear(self) -> None:
        _no_links(self.path)
        self.path.unlink(missing_ok=True)
