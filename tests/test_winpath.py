from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.core.winpath import extended_path, filesystem_path, remove_tree


@unittest.skipUnless(os.name == "nt", "Win32 extended paths are Windows-specific")
class WinPathTests(unittest.TestCase):
    def test_astral_file_below_cutoff_in_code_points_uses_extended_path(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="PhoneNAS-winpath-"))
        self.addCleanup(remove_tree, root)
        astral = "\U00020000" * 40
        padding = max(0, 230 - len(str(root / (astral + ".txt"))))
        path = root / (astral + "a" * padding + ".txt")
        if len(str(path)) >= 248:
            self.skipTest("temporary path is too long to straddle the Win32 cutoff")
        self.assertGreaterEqual(len(str(path).encode("utf-16-le")) // 2, 260)

        native_path = Path(extended_path(path))
        native_path.write_bytes(b"long path contents")
        resolved = filesystem_path(path)
        # This must hold even on hosts where unprefixed long paths are enabled.
        self.assertEqual(resolved, native_path)
        self.assertEqual(resolved.read_bytes(), b"long path contents")

    def test_unc_path_uses_extended_unc_spelling_at_248_utf16_units(self) -> None:
        # Exercise the spelling only; no share is accessed.
        share = "\\\\server.invalid\\share\\"
        at_cutoff = share + "\U00020000" + "a" * (248 - len(share) - 2)
        below_cutoff = at_cutoff[:-1]
        self.assertEqual(str(filesystem_path(below_cutoff)), below_cutoff)
        self.assertEqual(str(filesystem_path(at_cutoff)), "\\\\?\\UNC\\" + at_cutoff[2:])

    def test_extended_drive_and_unc_paths_keep_their_spelling(self) -> None:
        for path in (r"\\?\C:\work\file.txt", r"\\?\UNC\server.invalid\share\file.txt"):
            with self.subTest(path=path):
                self.assertEqual(str(filesystem_path(path)), path)
                self.assertEqual(extended_path(path), path)
