from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Config
from app.core.history import HistoryService
from app.core.rclone import Rclone
from app.core.sync import SyncEngine
from app.core.tasks import new_task


def digest_tree(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            with path.open("rb") as stream:
                result[path.relative_to(root).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def emit(step: str, **values) -> None:
    print(json.dumps({"step": step, **values}, ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--remote-root", required=True)
    arguments = parser.parse_args()
    root = arguments.remote_root
    if not root.startswith("PhoneNAS-diagnostics/") or len(root.split("/")) != 2:
        raise ValueError("Only one unique PhoneNAS-diagnostics child may be used")
    identity = root.split("/")[1]
    if len(identity) != 32 or uuid.UUID(identity).hex != identity:
        raise ValueError("Use an exclusive UUID diagnostic child")
    profile = Config(arguments.config)
    if profile.load_error:
        raise RuntimeError("Existing connection cannot be read")
    before = arguments.config.read_bytes()
    for key in list(os.environ):
        if key.startswith("RCLONE_"):
            os.environ.pop(key)
    with tempfile.TemporaryDirectory(prefix="PhoneNAS-real-DAV-test-") as temporary:
        base = Path(temporary)
        os.environ["PHONENAS_DATA_DIR"] = str(base / "app-data")
        os.environ["APPDATA"] = str(base / "profile")
        office, home = base / "ordinary-folder", base / "received-folder"
        office.mkdir()
        home.mkdir()
        (office / "docs").mkdir()
        (office / "docs" / "guide.txt").write_text("original document", encoding="utf-8")
        (office / "keep.txt").write_text("keep both sides", encoding="utf-8")
        (office / "removed.txt").write_text("old file for history", encoding="utf-8")
        publisher = replace(new_task(str(office), "DAV diagnostic", remote_path=f"{root}/mirror",
                                     versions_path=f"{root}/versions"), auto_sync=False)
        receiver = replace(new_task(str(home), "DAV receive diagnostic", direction="download",
                                    remote_path=publisher.remote_path, versions_path=publisher.versions_path,
                                    dataset_id=publisher.dataset_id), auto_sync=False)
        client = Rclone(profile.snapshot(), conf_path=base / "private.conf")
        with client.isolated_conf():
            client.ensure_conf()
            probe = client.run(["lsjson", f"phone:{root}", "--stat"], timeout=30)
            if probe.ok or probe.code not in (3, 4):
                raise RuntimeError("Diagnostic directory is not proven absent; no writes performed")
            emit("exclusive_test_target", root=root, dataset_id=publisher.dataset_id)

            def engine(task, operation=""):
                return SyncEngine(client, task, state_directory=base / "baselines",
                                  log_directory=base / "logs", operation=operation)

            source_before = digest_tree(office)
            upload = engine(publisher)
            first = upload.preview()
            if first.blocked:
                raise RuntimeError(first.reason)
            upload.execute(first)
            assert digest_tree(office) == source_before, "Upload must leave local source untouched"
            emit("upload", added=first.added, source_unchanged=True)
            pull = engine(receiver)
            first_pull = pull.preview()
            if first_pull.blocked:
                raise RuntimeError(first_pull.reason)
            pull.execute(first_pull)
            assert digest_tree(home) == source_before
            emit("download", added=first_pull.added, flat_relative_files=list(digest_tree(home)))
            (home / "docs" / "guide.txt").write_text("modified on receive PC", encoding="utf-8")
            (home / "added.txt").write_text("new work", encoding="utf-8")
            (home / "removed.txt").unlink()
            returned = engine(receiver, "return")
            changes = returned.preview()
            assert not changes.blocked, changes.reason
            stamp = returned.execute(changes)
            old_files = HistoryService(client, publisher).list_files(stamp)
            assert "docs/guide.txt" in old_files and "removed.txt" in old_files
            emit("manual_return", added=changes.added, updated=changes.updated, deleted=changes.deleted,
                 old_files_retained=True)
            adopt = engine(publisher, "adopt")
            adoption = adopt.preview()
            assert not adoption.blocked, adoption.reason
            adopt.execute(adoption)
            assert digest_tree(office) == digest_tree(home)
            second_pull = engine(receiver).preview()
            assert not second_pull.blocked and second_pull.unchanged == 3
            assert not (home / "mirror").exists() and not (home / "versions").exists()
            emit("repeated_download", unchanged=second_pull.unchanged, wrappers_downloaded=False,
                 source_and_destination_equal=True)
            for path in home.rglob("*"):
                if path.is_file():
                    path.unlink()
            cleared = engine(receiver).preview()
            assert cleared.blocked and "未交回" in cleared.reason
            emit("clear_test_destination_only", blocked=cleared.blocked, reason=cleared.reason,
                 added_from_phone=cleared.added)
    assert arguments.config.read_bytes() == before, "Production config must not change"
    emit("complete", production_config_unchanged=True, production_work_folder_untouched=True,
         production_mirror_untouched=True, test_root_retained=root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
