#!/usr/bin/env python3
"""Verify review ZIP structure and hashes; not a signature or secret scanner."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

from package_changes import MAX_SINGLE_BYTES, MAX_TOTAL_BYTES, is_blocked


def verify(path: Path) -> int:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [entry.filename for entry in infos]
        if len(names) != len(set(names)) or len({name.casefold() for name in names}) != len(names):
            raise ValueError("Duplicate or case-colliding ZIP entries")
        if any(entry.is_dir() or entry.file_size > MAX_SINGLE_BYTES for entry in infos):
            raise ValueError("Unexpected directory or oversized file")
        if sum(entry.file_size for entry in infos) > MAX_TOTAL_BYTES + 2 * MAX_SINGLE_BYTES:
            raise ValueError("Oversized review archive")
        if not {"manifest.json", "REVIEW.md"}.issubset(names):
            raise ValueError("Missing manifest or review")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest["format_version"] != 1:
            raise ValueError("Unknown manifest format")
        expected = {"manifest.json", "REVIEW.md"}
        for item in manifest["files"]:
            rel = item["path"]
            name = f"files/{rel}"
            if is_blocked(rel) or name in expected:
                raise ValueError("Unsafe or duplicate manifest path")
            expected.add(name)
            data = archive.read(name)
            if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ValueError(f"Hash or length mismatch: {rel}")
        if set(names) != expected:
            raise ValueError("ZIP members differ from manifest")
        for rel in manifest["deleted_files"]:
            if is_blocked(rel) or f"files/{rel}" in expected:
                raise ValueError("Unsafe or inconsistent deletion")
        for item in manifest["renamed_files"]:
            if item["from"] not in manifest["deleted_files"] or f"files/{item['to']}" not in expected:
                raise ValueError("Inconsistent rename")
        if archive.testzip() is not None:
            raise ValueError("ZIP CRC mismatch")
        return len(manifest["files"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    try:
        count = verify(args.archive)
    except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile, RuntimeError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2
    print(f"OK: {count} files verified; content still requires review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
