import hashlib
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
    LEGACY_MANIFEST_DOCSTRING, MANIFEST_DOCSTRING,
    OPERATIONAL_SCRIPT_RELATIVE_PATHS, RELEASE_DIGEST_DOMAIN, ManifestError,
    calculate_release_digest, calculate_source_digest, check_project, main,
    stamp_project,
)


MANIFEST_TEMPLATE = '''"""{docstring}"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "{build_id}"
SOURCE_SHA256 = "{source_hash}"
'''


class BuildManifestTests(unittest.TestCase):
    def _write_lifecycle_scripts(self, root: Path) -> None:
        for relative in OPERATIONAL_SCRIPT_RELATIVE_PATHS:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                f"# fixed fixture lifecycle: {path.name}\n".encode("utf-8")
            )

    def _project(
        self, root: Path, source_hash: str | None = None,
        build_id: str = "local-build-20260727.1",
    ) -> Path:
        package = root / "src" / "local_company"
        package.mkdir(parents=True)
        self._write_lifecycle_scripts(root)
        (root / "pyproject.toml").write_text(
            '[project]\nname = "fixture"\nversion = "0.1.0"\n', encoding="utf-8",
        )
        (package / "__init__.py").write_text(
            '__version__ = "0.1.0"\n', encoding="utf-8",
        )
        (package / "worker.py").write_bytes(b"def run():\n    return 1\n")
        if source_hash is None:
            source_hash = calculate_source_digest(package).sha256
        (package / "build_info.py").write_text(
            MANIFEST_TEMPLATE.format(
                docstring=LEGACY_MANIFEST_DOCSTRING,
                build_id=build_id,
                source_hash=source_hash,
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

    def test_release_digest_has_project_relative_framing_and_exact_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._project(root)
            nested = package / "nested"
            nested.mkdir()
            (nested / "build_info.py").write_bytes(b"nested = True\n")

            covered = [
                path for path in package.rglob("*.py")
                if path != package / "build_info.py"
            ] + [root / relative for relative in OPERATIONAL_SCRIPT_RELATIVE_PATHS]
            covered.sort(
                key=lambda path: path.relative_to(root).as_posix().encode("utf-8")
            )
            expected = hashlib.sha256()
            expected.update(RELEASE_DIGEST_DOMAIN)
            expected_bytes = 0
            for path in covered:
                relative = path.relative_to(root).as_posix().encode("utf-8")
                content = path.read_bytes()
                expected.update(len(relative).to_bytes(4, "big"))
                expected.update(relative)
                expected.update(len(content).to_bytes(8, "big"))
                expected.update(content)
                expected_bytes += len(content)

            release = calculate_release_digest(root)
            self.assertEqual(release.sha256, expected.hexdigest())
            self.assertEqual(
                release.sha256,
                "71f871359b5422ce800b66301efdb620894466aad241a38d9c7f3c94e4369ec9",
            )
            self.assertEqual(release.file_count, len(covered))
            self.assertEqual(release.total_bytes, expected_bytes)
            self.assertEqual(
                {path.relative_to(root).as_posix() for path in covered},
                {
                    "src/local_company/__init__.py",
                    "src/local_company/worker.py",
                    "src/local_company/nested/build_info.py",
                    *(path.as_posix() for path in OPERATIONAL_SCRIPT_RELATIVE_PATHS),
                },
            )

    def test_release_digest_tracks_each_fixed_script_and_excludes_unlisted_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._project(root)
            baseline = calculate_release_digest(root)

            for relative in OPERATIONAL_SCRIPT_RELATIVE_PATHS:
                path = root / relative
                original = path.read_bytes()
                with self.subTest(script=relative.as_posix()):
                    path.write_bytes(original + b"# lifecycle drift\n")
                    self.assertNotEqual(calculate_release_digest(root), baseline)
                    path.write_bytes(original)
                    self.assertEqual(calculate_release_digest(root), baseline)

            worker = package / "worker.py"
            original_worker = worker.read_bytes()
            worker.write_bytes(original_worker + b"# package drift\n")
            self.assertNotEqual(calculate_release_digest(root), baseline)
            worker.write_bytes(original_worker)

            manifest = package / "build_info.py"
            original_manifest = manifest.read_bytes()
            manifest.write_bytes(original_manifest + b"# excluded manifest drift\n")
            self.assertEqual(calculate_release_digest(root), baseline)
            manifest.write_bytes(original_manifest)

            (root / "scripts" / "unlisted.py").write_bytes(b"ignored = True\n")
            (root / "scripts" / "notes.txt").write_bytes(b"ignored\n")
            (package / "notes.txt").write_bytes(b"ignored\n")
            self.assertEqual(calculate_release_digest(root), baseline)

    def test_release_digest_requires_every_fixed_script_and_rejects_unsafe_type(self):
        for relative in OPERATIONAL_SCRIPT_RELATIVE_PATHS:
            with self.subTest(missing=relative.as_posix()), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                package = self._project(root)
                manifest = package / "build_info.py"
                before = manifest.read_bytes()
                (root / relative).unlink()
                with self.assertRaises(ManifestError):
                    calculate_release_digest(root)
                self.assertEqual(manifest.read_bytes(), before)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._project(root)
            manifest = package / "build_info.py"
            before = manifest.read_bytes()
            unsafe = root / OPERATIONAL_SCRIPT_RELATIVE_PATHS[0]
            unsafe.unlink()
            unsafe.mkdir()
            with self.assertRaisesRegex(ManifestError, "regular project file"):
                calculate_release_digest(root)
            self.assertEqual(manifest.read_bytes(), before)

    def test_exact_legacy_manifest_requires_higher_build_and_migrates_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._project(
                root, build_id="local-build-20260727.11",
            )
            manifest = package / "build_info.py"
            legacy = manifest.read_bytes()

            with self.assertRaisesRegex(ManifestError, "requires migration"):
                check_project(root)
            with self.assertRaisesRegex(ManifestError, "must advance"):
                stamp_project(root, "local-build-20260727.11")
            self.assertEqual(manifest.read_bytes(), legacy)
            self.assertEqual(tuple(package.glob(".build_info.py.*.tmp")), ())

            result = stamp_project(root, "local-build-20260727.12")
            self.assertTrue(result["changed"])
            self.assertEqual(result["previous_build_id"], "local-build-20260727.11")
            self.assertEqual(result["build_id"], "local-build-20260727.12")
            self.assertEqual(
                result["source_sha256"], calculate_release_digest(root).sha256,
            )
            current = manifest.read_text(encoding="utf-8")
            self.assertIn(f'"""{MANIFEST_DOCSTRING}"""', current)
            checked = check_project(root)
            self.assertEqual(checked["source_sha256"], result["source_sha256"])
            stamped = manifest.read_bytes()
            repeated = stamp_project(root, "local-build-20260727.12")
            self.assertFalse(repeated["changed"])
            self.assertEqual(manifest.read_bytes(), stamped)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._project(
                root, build_id="local-build-20260727.11",
            )
            manifest = package / "build_info.py"
            malformed_legacy = manifest.read_text(encoding="utf-8").replace(
                "The source digest covers", "A source digest covers", 1,
            )
            manifest.write_text(malformed_legacy, encoding="utf-8")
            before = manifest.read_bytes()
            with self.assertRaisesRegex(ManifestError, "docstring"):
                stamp_project(root, "local-build-20260727.12")
            self.assertEqual(manifest.read_bytes(), before)

    def test_lifecycle_script_change_between_release_scans_aborts_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._project(
                root, build_id="local-build-20260727.11",
            )
            manifest = package / "build_info.py"
            before = manifest.read_bytes()
            lifecycle = root / OPERATIONAL_SCRIPT_RELATIVE_PATHS[0]
            real_calculate = calculate_release_digest
            calls = 0

            def scan(project_root: Path):
                nonlocal calls
                digest = real_calculate(project_root)
                if calls == 0:
                    lifecycle.write_bytes(
                        lifecycle.read_bytes() + b"# changed between scans\n"
                    )
                calls += 1
                return digest

            with patch(
                "scripts.stamp_build_manifest.calculate_release_digest",
                side_effect=scan,
            ):
                with self.assertRaisesRegex(ManifestError, "between release scans"):
                    stamp_project(root, "local-build-20260727.12")
            self.assertEqual(calls, 2)
            self.assertEqual(manifest.read_bytes(), before)
            self.assertEqual(tuple(package.glob(".build_info.py.*.tmp")), ())

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
            self._write_lifecycle_scripts(project)
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
                    docstring=LEGACY_MANIFEST_DOCSTRING,
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
