from __future__ import annotations

import json
import os
import secrets
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from threading import Timer
from time import monotonic
from unittest.mock import patch

from app.core.config import Config
from app.core.relay import DatasetCoordinator
from app.core.rclone import Rclone, rclone_exe
from app.core.sync import SyncEngine
from app.core.tasks import new_task
from app.core.nas_tls import read_nas_trust


@contextmanager
def fixture(executable, scratch, user, password):
    env = dict(os.environ, TMP=str(scratch), TEMP=str(scratch), TMPDIR=str(scratch))
    process = subprocess.Popen([str(executable)], cwd=scratch, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    alarm = Timer(10, process.kill)
    try:
        process.stdin.write(json.dumps({"Username": user, "Password": password}).encode() + b"\n")
        process.stdin.flush()
        alarm.start()
        report = json.loads(process.stdout.readline(16 * 1024))
        alarm.cancel()
        root = Path(report["root"]).resolve()
        if root.parent != scratch.resolve() or not root.name.startswith("PhoneNAS-relay-fixture-"):
            raise RuntimeError("fixture returned a directory outside the test sandbox")
        yield report["url"], report["pem"], root
    finally:
        alarm.cancel()
        process.stdin.close()
        process.stdin = None
        try:
            process.communicate(timeout=6)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise RuntimeError("isolated DAV fixture did not stop") from None
        if process.returncode:
            raise RuntimeError("isolated DAV fixture returned an error")


class PinnedRelayIntegrationTests(unittest.TestCase):
    def test_publish_receive_return_adopt_on_go_https_with_real_locks(self):
        import ssl

        path = os.environ.get("PHONENAS_TEST_RELAY_FIXTURE")
        if not path or not rclone_exe().is_file():
            self.skipTest("build the isolated Android Go relayfixture and set PHONENAS_TEST_RELAY_FIXTURE")
        executable = Path(path).resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-paired-relay-") as directory:
            root = Path(directory)
            user, password = "fixture", secrets.token_urlsafe(32)
            office, home = root / "office", root / "home"
            office.mkdir()
            home.mkdir()
            name = "文件 #1.bin"
            old, edited = b"phone test original content", b"phone test edited content!!"
            large = secrets.token_bytes(8 << 20)
            (office / name).write_bytes(old)
            (office / "删除.txt").write_bytes(b"archive witness")
            (office / "large.bin").write_bytes(large)
            (office / "small").mkdir()
            for n in range(12):
                (office / "small" / f"{n}.txt").write_text(f"small file {n}", encoding="ascii")
            with patch.dict(os.environ, {"PHONENAS_DATA_DIR": str(root / "instance")}), fixture(
                executable, root, user, password
            ) as (url, pem, remote):
                fingerprint = sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
                trust = read_nas_trust(url, pem, fingerprint)
                clients = []
                for label in ("office", "home"):
                    config = Config(root / (label + ".json"))
                    config.set("version", 4)
                    config.set("nas.url", url)
                    config.set("nas.username", user)
                    config.set("nas.tls_certificate", trust.pem)
                    config.set("nas.tls_fingerprint", trust.fingerprint)
                    config.password = password
                    config.save()
                    client = Rclone(Config(config.path), root / (label + "-rclone.conf"))
                    client.ensure_conf()
                    clients.append(client)
                publish, receive = clients
                target = "dataset/mirror"
                history = "dataset/versions"
                source_task = new_task(str(office), remote_path=target, versions_path=history)
                receive_task = new_task(str(home), direction="download", remote_path=target, versions_path=history,
                                        dataset_id=source_task.dataset_id)

                def run(client, task, name, operation=""):
                    engine = SyncEngine(client, task, state_directory=root / (name + "-state"), operation=operation)
                    start = monotonic()
                    plan = engine.preview()
                    self.assertFalse(plan.blocked, plan.reason)
                    stamp = engine.execute(plan)
                    print(f"isolated {operation or name}: {monotonic() - start:.2f} s (preview, content checks and lock)")
                    self.assertFalse(DatasetCoordinator(client, task).locked())
                    return stamp

                run(publish, source_task, "publish")
                self.assertEqual(sha256((remote / "shared/mirror/large.bin").read_bytes()).digest(), sha256(large).digest())
                self.assertEqual(sha256((office / "large.bin").read_bytes()).digest(), sha256(large).digest())
                run(receive, receive_task, "receive")
                self.assertEqual(sha256((home / "large.bin").read_bytes()).digest(), sha256(large).digest())
                self.assertEqual((home / "small/11.txt").read_bytes(), b"small file 11")
                self.assertEqual((home / name).read_bytes(), old)
                (home / name).write_bytes(edited)
                (home / "删除.txt").unlink()
                (home / "added.txt").write_bytes(b"from receiver")
                blocked = SyncEngine(receive, receive_task, state_directory=root / "receive-state").preview()
                self.assertTrue(blocked.blocked)
                stamp = run(receive, receive_task, "receive", "return")
                self.assertEqual((remote / "shared/mirror" / name).read_bytes(), edited)
                self.assertEqual((remote / "shared/versions" / stamp / name).read_bytes(), old)
                self.assertEqual((remote / "shared/versions" / stamp / "删除.txt").read_bytes(), b"archive witness")
                self.assertEqual((office / name).read_bytes(), old)
                stale = SyncEngine(publish, source_task, state_directory=root / "publish-state").preview()
                self.assertTrue(stale.blocked)
                run(publish, source_task, "publish", "adopt")
                self.assertEqual((office / name).read_bytes(), edited)
                self.assertEqual((office / "added.txt").read_bytes(), b"from receiver")
                self.assertEqual(list((remote / "readonly").iterdir()), [])
            self.assertFalse(remote.exists())


if __name__ == "__main__":
    unittest.main()
