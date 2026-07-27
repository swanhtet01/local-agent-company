import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from scripts.stamp_build_manifest import (
    ManifestError, calculate_source_digest, check_project, main, stamp_project,
)


MANIFEST_TEMPLATE = '''"""Generated, read-only identity for the local runtime build.

The source digest covers every Python file in this package except this manifest.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "{build_id}"
SOURCE_SHA256 = "{source_hash}"
'''


class BuildManifestTests(unittest.TestCase):
    def _project(self, root: Path, source_hash: str = "0" * 64) -> Path:
        package = root / "src" / "local_company"
        package.mkdir(parents=True)
        (root / "pyproject.toml").write_text(
            '[project]\nname = "fixture"\nversion = "0.1.0"\n', encoding="utf-8",
        )
        (package / "__init__.py").write_text(
            '__version__ = "0.1.0"\n', encoding="utf-8",
        )
        (package / "worker.py").write_bytes(b"def run():\n    return 1\n")
        (package / "build_info.py").write_text(
            MANIFEST_TEMPLATE.format(
                build_id="local-build-20260727.1", source_hash=source_hash,
            ),
            encoding="utf-8",
        )
        return package

    def test_digest_has_frozen_framing_and_excludes_only_root_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "local_company"
            (source / "nested").mkdir(parents=True)
            files = {
                "nested/z.py": b"\x00B\n",
                "nested/build_info.py": b"nested = True\n",
                "a.py": b"A\r\n",
                "__init__.py": b"",
            }
            for relative in reversed(tuple(files)):
                (source / relative).write_bytes(files[relative])
            (source / "build_info.py").write_text("ignored = 1\n", encoding="utf-8")

            first = calculate_source_digest(source)
            self.assertEqual(
                first.sha256,
                "affc58421faebc544474a99c40b0c3e0dfcf6af614bc12a4db2d6299e8c938f6",
            )
            self.assertEqual(first.file_count, 4)
            (source / "build_info.py").write_text("ignored = 2\n", encoding="utf-8")
            self.assertEqual(calculate_source_digest(source), first)

            nested_manifest = source / "nested" / "build_info.py"
            nested_manifest.write_bytes(b"nested = False\n")
            self.assertNotEqual(calculate_source_digest(source), first)
            nested_manifest.write_bytes(files["nested/build_info.py"])
            (source / "new.py").write_bytes(b"new = True\n")
            self.assertNotEqual(calculate_source_digest(source), first)
            (source / "new.py").unlink()
            (source / "a.py").rename(source / "renamed.py")
            self.assertNotEqual(calculate_source_digest(source), first)
            (source / "renamed.py").rename(source / "a.py")
            self.assertEqual(calculate_source_digest(source), first)

            with patch("scripts.stamp_build_manifest.MAX_SOURCE_FILE_BYTES", 2):
                with self.assertRaises(ManifestError):
                    calculate_source_digest(source)

            outside = Path(tmp) / "outside.py"
            outside.write_text("sentinel = True\n", encoding="utf-8")
            link = source / "external.py"
            try:
                link.symlink_to(outside)
            except OSError:
                pass
            else:
                with self.assertRaises(ManifestError):
                    calculate_source_digest(source)
                self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel = True\n")

    def test_stamp_is_atomic_monotonic_and_rejects_executable_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._project(root)
            manifest = package / "build_info.py"
            original = manifest.read_bytes()
            protected = {
                path: path.read_bytes()
                for path in (
                    root / "pyproject.toml",
                    package / "__init__.py",
                    package / "worker.py",
                )
            }

            with self.assertRaises(ManifestError):
                stamp_project(root, "local-build-20260727.1")
            self.assertEqual(manifest.read_bytes(), original)
            self.assertFalse(tuple(package.glob(".build_info.py.*.tmp")))
            with patch(
                "scripts.stamp_build_manifest.os.replace",
                side_effect=OSError("injected replace failure"),
            ):
                with self.assertRaises(ManifestError) as failed_replace:
                    stamp_project(root, "local-build-20260727.2")
            self.assertFalse(failed_replace.exception.replacement_committed)
            self.assertEqual(manifest.read_bytes(), original)
            self.assertFalse(tuple(package.glob(".build_info.py.*.tmp")))
            for invalid in ("local-build-20260230.2", "local-build-20260727.0"):
                with self.assertRaises(ManifestError):
                    stamp_project(root, invalid)
                self.assertEqual(manifest.read_bytes(), original)

            result = stamp_project(root, "local-build-20260727.2")
            self.assertTrue(result["changed"])
            checked = check_project(root)
            self.assertEqual(checked["build_id"], "local-build-20260727.2")
            self.assertEqual(checked["source_sha256"], result["source_sha256"])
            stamped = manifest.read_bytes()
            repeated = stamp_project(root, "local-build-20260727.2")
            self.assertFalse(repeated["changed"])
            self.assertEqual(manifest.read_bytes(), stamped)
            with self.assertRaises(ManifestError):
                stamp_project(root, "local-build-20260726.9")
            for path, content in protected.items():
                self.assertEqual(path.read_bytes(), content)

            (package / "worker.py").write_bytes(b"def run():\n    return 2\n")
            with patch(
                "scripts.stamp_build_manifest.check_project",
                side_effect=ManifestError("injected post-update failure"),
            ):
                with self.assertRaises(ManifestError) as failed_check:
                    stamp_project(root, "local-build-20260727.3")
            self.assertTrue(failed_check.exception.replacement_committed)
            self.assertEqual(check_project(root)["build_id"], "local-build-20260727.3")

            marker = root / "must-not-exist"
            manifest.write_text(
                manifest.read_text(encoding="utf-8")
                + f'\n__import__("pathlib").Path({str(marker)!r}).write_text("bad")\n',
                encoding="utf-8",
            )
            with self.assertRaises(ManifestError):
                check_project(root)
            self.assertFalse(marker.exists())

    def test_intermediate_source_link_cannot_redirect_manifest_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "project"
            project.mkdir()
            (project / "pyproject.toml").write_text(
                '[project]\nname = "fixture"\nversion = "0.1.0"\n', encoding="utf-8",
            )
            external_src = base / "external-src"
            package = external_src / "local_company"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text(
                '__version__ = "0.1.0"\n', encoding="utf-8",
            )
            (package / "worker.py").write_text("value = 1\n", encoding="utf-8")
            manifest = package / "build_info.py"
            manifest.write_text(
                MANIFEST_TEMPLATE.format(
                    build_id="local-build-20260727.1", source_hash="0" * 64,
                ),
                encoding="utf-8",
            )
            before = manifest.read_bytes()
            try:
                (project / "src").symlink_to(external_src, target_is_directory=True)
            except OSError as exc:
                if sys.platform != "win32":
                    self.skipTest(f"directory symlink unavailable: {exc}")
                junction = subprocess.run(
                    [
                        "cmd.exe", "/d", "/c", "mklink", "/J",
                        str(project / "src"), str(external_src),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if junction.returncode != 0:
                    self.skipTest(f"directory junction unavailable: {junction.stderr}")
            with self.assertRaises(ManifestError):
                check_project(project)
            with self.assertRaises(ManifestError):
                stamp_project(project, "local-build-20260727.2")
            self.assertEqual(manifest.read_bytes(), before)

    def test_cli_check_is_cwd_independent_and_read_only(self):
        project_root = Path(__file__).parents[1]
        manifest = project_root / "src" / "local_company" / "build_info.py"
        before = manifest.read_bytes()
        before_mtime = manifest.stat().st_mtime_ns
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(project_root / "scripts" / "stamp_build_manifest.py"),
                    "--check",
                ],
                cwd=tmp,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["mode"], "check")
        self.assertFalse(result["changed"])
        invalid = subprocess.run(
            [
                sys.executable,
                str(project_root / "scripts" / "stamp_build_manifest.py"),
                "--write",
                "--build-id",
                "local-build-20260230.2",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual(manifest.stat().st_mtime_ns, before_mtime)

        error_stream = io.StringIO()
        with (
            patch(
                "scripts.stamp_build_manifest.stamp_project",
                side_effect=ManifestError(
                    "injected post-replace error", replacement_committed=True,
                ),
            ),
            redirect_stderr(error_stream),
        ):
            self.assertEqual(
                main(["--write", "--build-id", "local-build-20260727.2"]), 4,
            )
        committed_error = json.loads(error_stream.getvalue())
        self.assertTrue(committed_error["changed"])
        self.assertTrue(committed_error["replacement_committed"])
        self.assertIn("recovery", committed_error)

        error_stream = io.StringIO()
        with (
            patch(
                "scripts.stamp_build_manifest.check_project",
                side_effect=OSError("injected top-level filesystem error"),
            ),
            redirect_stderr(error_stream),
        ):
            self.assertEqual(main(["--check"]), 3)
        filesystem_error = json.loads(error_stream.getvalue())
        self.assertFalse(filesystem_error["changed"])
        self.assertFalse(filesystem_error["replacement_committed"])


if __name__ == "__main__":
    unittest.main()
