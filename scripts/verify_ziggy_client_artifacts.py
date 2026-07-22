#!/usr/bin/env python3
"""Verify that the built Ziggy PWA is present in a Python wheel."""

from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZipFile

REQUIRED_DIST_FILES = (
    "index.html",
    "manifest.webmanifest",
    "sw.js",
)
WHEEL_DIST_PREFIX = "nanobot/web/dist/"


def contains_bytes(root: Path, needle: bytes) -> bool:
    for path in root.rglob("*"):
        if path.is_file():
            try:
                if needle in path.read_bytes():
                    return True
            except OSError:
                continue
    return False


def verify_dist(dist: Path, publishable_key: str) -> None:
    if not dist.is_dir():
        raise ValueError(f"PWA build directory does not exist: {dist}")

    missing = [name for name in REQUIRED_DIST_FILES if not (dist / name).is_file()]
    if missing:
        raise ValueError(f"PWA build is missing required files: {', '.join(missing)}")

    if not any(path.suffix == ".js" for path in (dist / "assets").glob("*.js")):
        raise ValueError("PWA build does not contain a JavaScript asset")

    if not contains_bytes(dist, publishable_key.encode("utf-8")):
        raise ValueError("PWA build does not contain the explicitly supplied Clerk key")


def verify_wheel(wheel: Path, publishable_key: str) -> None:
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise ValueError(f"Python wheel does not exist: {wheel}")

    with ZipFile(wheel) as archive:
        names = set(archive.namelist())
        missing = [
            f"{WHEEL_DIST_PREFIX}{name}"
            for name in REQUIRED_DIST_FILES
            if f"{WHEEL_DIST_PREFIX}{name}" not in names
        ]
        if missing:
            raise ValueError(f"Python wheel is missing PWA files: {', '.join(missing)}")

        if not any(
            name.startswith(f"{WHEEL_DIST_PREFIX}assets/") and name.endswith(".js")
            for name in names
        ):
            raise ValueError("Python wheel does not contain a PWA JavaScript asset")

        if not any(
            publishable_key.encode("utf-8") in archive.read(name)
            for name in names
            if name.startswith(WHEEL_DIST_PREFIX) and not name.endswith("/")
        ):
            raise ValueError("Python wheel does not preserve the Clerk key in the PWA")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--publishable-key", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.publishable_key.startswith(("pk_test_", "pk_live_")):
        raise ValueError("verification requires a Clerk publishable key")

    verify_dist(args.dist, args.publishable_key)
    verify_wheel(args.wheel, args.publishable_key)
    print("Verified PWA assets and Clerk configuration in the Python wheel")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
