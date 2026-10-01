from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from deploy.verify_transfer_package import PackageError, verify_package


class TransferPackageTests(unittest.TestCase):
    def package(self, root: Path, *, unsafe: bool = False, failed_tests: bool = False) -> Path:
        source = root / "workcell-source.zip"
        deploy = root / "workcell-deploy.zip"
        source_content = {"README.md": b"source", "src/local_company/build_info.py": b"old"}
        deploy_content = {**source_content, "src/local_company/build_info.py": b"new"}
        if unsafe:
            source_content["../escape"] = b"bad"
            deploy_content["../escape"] = b"bad"
        for path, content in ((source, source_content), (deploy, deploy_content)):
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, value in content.items():
                    archive.writestr(name, value)
        digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        commit = "a" * 40
        (root / "HANDOFF.md").write_text(f"Source commit: `{commit[:7]}`\n", encoding="utf-8")
        (root / "verification.json").write_text(json.dumps({
            "sourceCommit": commit,
            "sourceArchiveSha256": digest(source),
            "deploymentSha256": digest(deploy),
            "archiveFiles": len(source_content),
            "changedFromGitArchive": ["src/local_company/build_info.py"],
            "serverAcceptance": "NOT RUN",
            "localExtractedTests": {"status": "failed" if failed_tests else "passed",
                                     "errors": 0, "failures": 0, "tests_run": 10},
        }), encoding="utf-8")
        return root

    def test_valid_package_is_verified_without_accepting_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = verify_package(self.package(Path(directory)))
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["changed_files"], ["src/local_company/build_info.py"])
        self.assertEqual(result["server_acceptance"], "NOT RUN")
        self.assertFalse(result["external_mutation_performed"])

    def test_digest_tampering_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.package(Path(directory))
            with (root / "workcell-deploy.zip").open("ab") as destination:
                destination.write(b"tamper")
            with self.assertRaisesRegex(PackageError, "archive_digest_mismatch"):
                verify_package(root)

    def test_unsafe_member_and_failed_test_claim_fail_closed(self) -> None:
        for unsafe, failed in ((True, False), (False, True)):
            with self.subTest(unsafe=unsafe, failed=failed), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(PackageError):
                    verify_package(self.package(Path(directory), unsafe=unsafe, failed_tests=failed))

    def test_unexpected_package_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.package(Path(directory))
            (root / "secret.env").write_text("PRIVATE_SENTINEL", encoding="utf-8")
            with self.assertRaisesRegex(PackageError, "package_file_set_invalid"):
                verify_package(root)


if __name__ == "__main__":
    unittest.main()
