#!/usr/bin/env python3
"""Resolve the newest eval report without a SIGPIPE-prone shell pipeline."""

from __future__ import annotations

import re
import sys
from pathlib import Path


VALID_STEM = re.compile(r"^[a-z0-9_]+$")


def resolve_latest(reports_dir: Path, stem: str) -> Path:
    if not VALID_STEM.fullmatch(stem):
        raise ValueError("invalid report stem")

    candidates = list(reports_dir.glob(f"{stem}_*.json"))
    if not candidates:
        raise FileNotFoundError(f"no reports found for {stem}")

    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: resolve_latest_report.py REPORTS_DIR STEM", file=sys.stderr)
        return 2

    try:
        print(resolve_latest(Path(argv[1]), argv[2]))
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
