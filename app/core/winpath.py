from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


_LOG = logging.getLogger(__name__)


def extended_path(value: str | os.PathLike[str]) -> str:
    r"""Return a Win32 extended-length spelling for filesystem API calls.

    The 260 UTF-16 code-unit limit applies to several Python/Windows directory
    APIs even when rclone itself can handle longer paths.  The ``\\?\`` prefix
    bypasses that limit; Unix paths are returned unchanged.
    """
    text = os.fspath(value)
    if os.name != "nt":
        return os.path.abspath(text)
    if text.startswith("\\\\?\\"):
        return text
    text = os.path.abspath(text)
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def filesystem_path(value: str | os.PathLike[str]) -> Path:
    r"""Return a Path that Windows can address, using an extended spelling only
    when the ordinary absolute path is close to MAX_PATH.

    Keeping short paths in their original spelling preserves ordinary Win32
    alias checks and test seams; long paths get the ``\\?\`` form.
    """
    text = os.fspath(value)
    if os.name != "nt" or text.startswith("\\\\?\\"):
        return Path(text)
    absolute = os.path.abspath(text)
    # Win32 counts UTF-16 code units; supplementary characters take two.
    if len(absolute.encode("utf-16-le", errors="surrogatepass")) // 2 < 248:
        return Path(text)
    return Path(extended_path(absolute))


def remove_tree(value: str | os.PathLike[str], *, attempts: int = 8) -> None:
    """Remove a tree, including paths beyond MAX_PATH, with short retries.

    A just-closed rclone process or a scanner can briefly keep a directory
    entry alive on Windows.  Retrying the whole tree avoids masking the real
    operation with a transient WinError 145.
    """
    path = extended_path(value)
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.05 * (attempt + 1))


@contextmanager
def staging_directory(prefix: str = "PhoneNAS-staged-"):
    """Provide a disposable staging root and clean it up safely.

    Cleanup failures must not turn an already completed transfer into a
    reported failure; the leftover path is logged for manual inspection.
    """
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    finally:
        try:
            remove_tree(path)
        except OSError as exc:
            _LOG.warning("无法清理临时暂存目录 %s：%s", path, exc)
