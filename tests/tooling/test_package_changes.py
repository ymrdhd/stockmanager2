"""Integration tests for the code-review ZIP workflow (no external deps)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SOURCE_TOOL = Path(__file__).resolve().parents[2] / "tools" / "package_changes.py"


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


class PackageChangesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "tools").mkdir()
        shutil.copy2(SOURCE_TOOL, self.root / "tools" / "package_changes.py")
        shutil.copy2(SOURCE_TOOL.with_name("verify_review.py"), self.root / "tools" / "verify_review.py")
        (self.root / ".gitignore").write_text("artifacts/\ndata/\n.env\n", encoding="utf-8")

    def run_tool(self, *args: str) -> tuple[subprocess.CompletedProcess[str], Path | None]:
        proc = subprocess.run(
            [sys.executable, str(self.root / "tools" / "package_changes.py"), *args,
             "--summary", "Test change", "--tests", "unittest: PASS"],
            capture_output=True, text=True,
        )
        path = None
        if proc.returncode == 0:
            path = Path(proc.stdout.split("ARCHIVE: ", 1)[1].strip())
        return proc, path

    def test_incremental_added_modified_deleted(self) -> None:
        git(self.root, "init", "-q")
        git(self.root, "config", "user.name", "Agent Test")
        git(self.root, "config", "user.email", "agent@example.invalid")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "spec.md").write_text("v1", encoding="utf-8")
        (self.root / "old.txt").write_text("remove", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "base", "-q")
        (self.root / "docs" / "spec.md").write_text("v2", encoding="utf-8")
        (self.root / "backend").mkdir()
        (self.root / "backend" / "main.py").write_text("print('hello')\n", encoding="utf-8")
        (self.root / "old.txt").unlink()
        (self.root / "data").mkdir()
        (self.root / "data" / "portfolio.db").write_text("PRIVATE", encoding="utf-8")
        (self.root / ".env").write_text("SECRET=x", encoding="utf-8")
        proc, package = self.run_tool("--base", "HEAD")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNotNone(package)
        with zipfile.ZipFile(package) as z:
            names = z.namelist()
            self.assertIn("files/docs/spec.md", names)
            self.assertIn("files/backend/main.py", names)
            self.assertIn("manifest.json", names)
            self.assertIn("REVIEW.md", names)
            self.assertNotIn("files/data/portfolio.db", names)
            self.assertNotIn("files/.env", names)
            manifest = json.loads(z.read("manifest.json"))
            self.assertEqual(manifest["deleted_files"], ["old.txt"])
            self.assertEqual({x["path"] for x in manifest["files"]},
                             {"docs/spec.md", "backend/main.py"})

    def test_initial_snapshot_excludes_private_files(self) -> None:
        (self.root / "README.md").write_text("a", encoding="utf-8")
        (self.root / "data").mkdir()
        (self.root / "data" / "account.db").write_bytes(b"hidden")
        (self.root / ".env").write_text("SECRET", encoding="utf-8")
        proc, package = self.run_tool("--all")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with zipfile.ZipFile(package) as z:
            self.assertIn("files/README.md", z.namelist())
            self.assertNotIn("files/.env", z.namelist())
            self.assertNotIn("files/data/account.db", z.namelist())

    def test_symlink_is_not_exported(self) -> None:
        secret = self.root / "secret.txt"
        secret.write_text("private", encoding="utf-8")
        link = self.root / "linked.txt"
        try:
            link.symlink_to(secret)
        except OSError:
            self.skipTest("Symlinks not permitted")
        proc, package = self.run_tool("--all")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with zipfile.ZipFile(package) as z:
            self.assertNotIn("files/linked.txt", z.namelist())


    def test_verifier_rejects_extra_and_tampered_content(self) -> None:
        (self.root / "README.md").write_text("original", encoding="utf-8")
        proc, package = self.run_tool("--all")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        command = [sys.executable, str(self.root / "tools" / "verify_review.py")]
        self.assertEqual(subprocess.run(command + [str(package)], capture_output=True).returncode, 0)
        for variant in ("extra", "tampered"):
            altered = self.root / f"{variant}.zip"
            with zipfile.ZipFile(package) as src, zipfile.ZipFile(altered, "w") as dst:
                for name in src.namelist():
                    data = src.read(name)
                    if variant == "tampered" and name == "files/README.md":
                        data = b"modified"
                    dst.writestr(name, data)
                if variant == "extra":
                    dst.writestr("unexpected.txt", b"unexpected")
            self.assertNotEqual(subprocess.run(command + [str(altered)], capture_output=True).returncode, 0)

    def test_explicit_base_required(self) -> None:
        proc, _ = self.run_tool()
        self.assertNotEqual(proc.returncode, 0)

    def test_committed_and_uncommitted_changes_from_start(self) -> None:
        git(self.root, "init", "-q")
        git(self.root, "config", "user.name", "Agent Test")
        git(self.root, "config", "user.email", "agent@example.invalid")
        (self.root / "README.md").write_text("base", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "base")
        base = subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True).strip()
        (self.root / "README.md").write_text("committed", encoding="utf-8")
        git(self.root, "add", "README.md")
        git(self.root, "commit", "-qm", "round change")
        (self.root / "token_service.py").write_text("# harmless source", encoding="utf-8")
        proc, package = self.run_tool("--base", base)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with zipfile.ZipFile(package) as z:
            manifest = json.loads(z.read("manifest.json"))
            self.assertEqual(manifest["baseline"], base)
            self.assertEqual({f["path"] for f in manifest["files"]}, {"README.md", "token_service.py"})


if __name__ == "__main__":
    unittest.main()
