#!/usr/bin/env python3
"""Package only this iteration's code/doc changes for external review.

Usage:
  python tools/package_changes.py --base <start-commit> --summary '...' --tests '...'
  python tools/package_changes.py --all --summary 'initial handoff' --tests '...'

The generated ZIP is NOT a database backup. It never includes runtime data,
secrets, symlinks, build artifacts, dependencies or existing review archives.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "artifacts" / "reviews"
MAX_SINGLE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 12 * 1024 * 1024
BLOCKED_DIRS = {
    ".git", ".venv", "venv", "node_modules", "dist", "build", "coverage",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea",
    ".vscode", "data", "cache", "logs", "backups", "artifacts", "secrets",
    "private", "tmp", "temp", "uploads",
}
BLOCKED_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".zip", ".7z", ".rar", ".bak",
    ".pem", ".key", ".p12", ".pfx", ".log", ".pyc", ".pyo",
    ".parquet", ".xlsx", ".xls", ".csv",
}


def run_git(*args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace").strip() or "Git command failed")
    return proc.stdout


def is_blocked(path: str) -> bool:
    # Defensive check: this function also runs on paths returned from git.
    posix = path.replace("\\", "/")
    p = PurePosixPath(posix)
    if not posix or p.is_absolute() or any(part in {"", ".", ".."} for part in p.parts):
        return True
    low = [part.lower() for part in p.parts]
    name = low[-1]
    if any(part in BLOCKED_DIRS for part in low[:-1]):
        return True
    if ":" in posix or "\\" in path or name in {".env", "id_rsa", "id_ed25519", "credentials.json", "secrets.json", "opencode.json"}:
        return True
    if name.startswith(".env.") and name != ".env.example":
        return True
    if name.endswith(("-wal", "-shm", ".sqlite-wal", ".sqlite-shm")):
        return True
    if Path(name).suffix.lower() not in {".py", ".ts", ".tsx", ".js", ".vue", ".md"} and any(
        keyword in name for keyword in ("secret", "password", "credential", "private_key", "api_token", "access_token")
    ):
        return True
    if Path(name).suffix.lower() in BLOCKED_SUFFIXES:
        return True
    # .gitignore, .env.example and standard text project config are allowed.
    return False


def valid_file(path: str) -> tuple[Path, int] | None:
    if is_blocked(path):
        return None
    file = ROOT / path
    # Never pack symlinks, including a symlinked parent directory.
    candidate = ROOT
    for part in PurePosixPath(path).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            return None
    if not file.is_file():
        return None
    if not file.resolve().is_relative_to(ROOT.resolve()):
        return None
    size = file.stat().st_size
    if size > MAX_SINGLE_BYTES:
        raise ValueError(f"Review file is too large ({size} bytes): {path}")
    return file, size


def git_changes(base: str) -> tuple[dict[str, str], list[str], list[dict[str, str]]]:
    # -z means names containing spaces, tabs and newlines are unambiguous.
    raw = run_git("diff", "--name-status", "-z", "--find-renames", base, "--")
    pieces = [x.decode("utf-8", errors="surrogateescape") for x in raw.split(b"\x00") if x]
    changes: dict[str, str] = {}
    deleted: list[str] = []
    renamed: list[dict[str, str]] = []
    i = 0
    while i < len(pieces):
        status = pieces[i]
        i += 1
        if status.startswith(("R", "C")):
            if i + 1 >= len(pieces):
                raise RuntimeError("Unexpected git rename/copy status")
            old, new = pieces[i], pieces[i + 1]
            i += 2
            if status.startswith("R"):
                if not is_blocked(old):
                    deleted.append(old)
                if not is_blocked(old) and not is_blocked(new):
                    renamed.append({"from": old, "to": new})
            changes[new] = "renamed" if status.startswith("R") else "copied"
            continue
        if i >= len(pieces):
            raise RuntimeError("Unexpected git diff status")
        path = pieces[i]
        i += 1
        if status.startswith("D"):
            deleted.append(path)
        else:
            changes[path] = {
                "A": "added", "M": "modified", "T": "modified",
                "U": "conflicted",
            }.get(status[:1], "modified")
    # Untracked, non-ignored files are part of the iteration as well.
    untracked = run_git("ls-files", "--others", "--exclude-standard", "-z")
    for b in untracked.split(b"\x00"):
        if b:
            path = b.decode("utf-8", errors="surrogateescape")
            changes.setdefault(path, "added")
    return changes, deleted, renamed


def all_files() -> tuple[dict[str, str], list[str], list[dict[str, str]]]:
    changes: dict[str, str] = {}
    for current, dirs, files in os.walk(ROOT, followlinks=False):
        dirs[:] = [d for d in dirs if d.lower() not in BLOCKED_DIRS
                   and not is_blocked((Path(current) / d / "dummy").relative_to(ROOT).as_posix())
                   and not (Path(current) / d).is_symlink()]
        for name in files:
            path = (Path(current) / name).relative_to(ROOT).as_posix()
            if not is_blocked(path):
                changes[path] = "initial"
    return changes, [], []


def short_head() -> str:
    try:
        return run_git("rev-parse", "--short=12", "HEAD").decode().strip()
    except RuntimeError:
        return "no-git"


def build_zip(*, base: str | None, all_mode: bool, summary: str, tests: str,
              risks: str = "未说明", rollback: str = "未说明") -> Path:
    if not all_mode:
        if not base:
            raise ValueError("Specify --base with the iteration start commit")
        base = run_git("rev-parse", "--verify", "--end-of-options", base + "^{commit}").decode().strip()
        run_git("merge-base", "--is-ancestor", base, "HEAD")
    changes, deleted, renamed = all_files() if all_mode else git_changes(base or "HEAD")
    selected: list[tuple[str, str, Path, int]] = []
    filtered_count = 0
    total = 0
    for path, change_type in sorted(changes.items()):
        found = valid_file(path)
        if found is None:
            filtered_count += 1
            continue
        file, size = found
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ValueError("Review package too large; split this iteration into smaller changes")
        selected.append((path, change_type, file, size))
    safe_deleted = sorted(set(d for d in deleted if not is_blocked(d)))
    safe_renamed = sorted((r for r in renamed if not is_blocked(r["from"]) and not is_blocked(r["to"])),
                          key=lambda r: (r["from"], r["to"]))
    if not selected and not safe_deleted:
        raise ValueError("No reviewable changes found; specify the iteration's start commit via --base")

    now = datetime.now(timezone.utc)
    stamped = now.strftime("%Y%m%dT%H%M%S%fZ")
    head = short_head()
    fingerprint = hashlib.sha256((str(base) + str([(p, t) for p, t, _, _ in selected]) + stamped)
                                 .encode()).hexdigest()[:8]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    archive = OUTPUT_DIR / f"{stamped}_{head}_{fingerprint}.zip"
    manifest = {
        "format_version": 1,
        "project": "Stock Manager 2.0",
        "created_at_utc": now.isoformat(),
        "mode": "initial" if all_mode else "incremental",
        "baseline": "initial snapshot" if all_mode else (base or "HEAD"),
        "head_at_packaging": head,
        "summary": summary,
        "tests": tests,
        "risks": risks,
        "rollback": rollback,
        "files": [],
        "deleted_files": safe_deleted,
        "renamed_files": safe_renamed,
        "filtered_paths_count": filtered_count,
    }
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=8) as z:
        for path, change_type, file, size in selected:
            data = file.read_bytes()
            if len(data) != size:
                raise RuntimeError(f"File changed during packaging: {path}")
            digest = hashlib.sha256(data).hexdigest()
            z.writestr(f"files/{path}", data)
            manifest["files"].append({"path": path, "change_type": change_type,
                                      "bytes": size, "sha256": digest})
        review = (
            "# 本轮代码审查说明\n\n"
            f"- 基准：`{manifest['baseline']}`\n"
            f"- 打包时 HEAD：`{head}`\n"
            f"- 目的/内容：{summary}\n"
            f"- 测试：{tests}\n"
            f"- 新增/修改文件：{len(selected)}\n"
            f"- 删除文件：{len(safe_deleted)}\n"
            f"- 更名文件：{len(safe_renamed)}\n"
            f"- 风险：{risks}\n"
            f"- 回退：{rollback}\n"
            f"- 过滤路径数：{filtered_count}（需人工核对排除项，过滤不是秘密扫描）\n\n"
            "## 文件清单\n\n"
            + "\n".join(f"- `{x['change_type']}` `{x['path']}`" for x in manifest["files"])
            + ("\n\n## 删除\n\n" + "\n".join(f"- `{p}`" for p in safe_deleted) if safe_deleted else "")
            + "\n"
        )
        z.writestr("REVIEW.md", review)
        z.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8"))
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description="Pack changed project files for agent review")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--all", action="store_true", help="Initial complete project snapshot only")
    mode.add_argument("--base", metavar="COMMIT", help="Commit at start of this iteration")
    parser.add_argument("--summary", required=True, help="What changed in this iteration")
    parser.add_argument("--tests", required=True, help="Commands, PASS/FAIL/NOT RUN and reason")
    parser.add_argument("--risks", default="未说明（审查前补充）")
    parser.add_argument("--rollback", default="未说明（审查前补充）")
    args = parser.parse_args()
    try:
        archive = build_zip(base=args.base, all_mode=args.all, summary=args.summary, tests=args.tests, risks=args.risks, rollback=args.rollback)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"ARCHIVE: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
