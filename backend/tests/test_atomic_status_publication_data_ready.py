"""Focused controls for status publication from a certified extraction."""

from __future__ import annotations

import json
import os
import pathlib
import stat
import tempfile
import unittest

import run


class AtomicStatusPublicationTests(unittest.TestCase):
    def test_atomic_helpers_replace_read_only_last_good_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            json_path = root / "run_status.json"
            text_path = root / "RUN_STATUS.md"
            json_path.write_text('{"status":"last-good"}\n', encoding="utf-8")
            text_path.write_text("last-good\n", encoding="utf-8")
            json_path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            text_path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

            run._atomic_json(json_path, {"status": "complete", "exit_code": 0})
            run._atomic_text(text_path, "complete\n")

            self.assertEqual("complete", json.loads(json_path.read_text())["status"])
            self.assertEqual("complete\n", text_path.read_text(encoding="utf-8"))
            self.assertFalse(any(p.name.endswith(".tmp") for p in root.iterdir()))

    def test_failed_replace_preserves_last_good_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            target = root / "RUN_STATUS.md"
            target.write_text("last-good\n", encoding="utf-8")
            original_replace = os.replace

            def fail_replace(src, dst):
                raise OSError("injected publish failure")

            os.replace = fail_replace
            try:
                with self.assertRaisesRegex(OSError, "injected publish failure"):
                    run._atomic_text(target, "new-but-unpublished\n")
            finally:
                os.replace = original_replace

            self.assertEqual("last-good\n", target.read_text(encoding="utf-8"))
            self.assertFalse(any(p.name.endswith(".tmp") for p in root.iterdir()))


if __name__ == "__main__":
    unittest.main()
