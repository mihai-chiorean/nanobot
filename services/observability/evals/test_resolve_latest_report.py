from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from resolve_latest_report import resolve_latest


class ResolveLatestReportTests(unittest.TestCase):
    def test_selects_newest_report_from_large_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reports = Path(directory)
            for index in range(256):
                report = reports / f"sensitive_paths_v0_{index:04d}.json"
                report.write_text("{}\n", encoding="utf-8")
                timestamp = time.time_ns() + index
                os.utime(report, ns=(timestamp, timestamp))

            latest = resolve_latest(reports, "sensitive_paths_v0")

            self.assertEqual(latest.name, "sensitive_paths_v0_0255.json")

    def test_rejects_invalid_stem(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                resolve_latest(Path(directory), "../secret")

    def test_reports_missing_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                resolve_latest(Path(directory), "shell_prescreen_v0")


if __name__ == "__main__":
    unittest.main()
