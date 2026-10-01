#!/usr/bin/env python3
"""Verify a SuperMega private-worker transfer package without extracting it."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

SCHEMA = "supermega.workcell-transfer-verification.v1"
EXPECTED_FILES = {"HANDOFF.md", "verification.json", "workcell-deploy.zip", "workcell-source.zip"}
MAX_RECEIPT_BYTES = 64 * 1024
MAX_HANDOFF_BYTES = 128 * 1024
MAX_ARCHIVE_FILES = 5000
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")


class PackageError(ValueError):
    pass


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _safe_member(name: str) -> bool:
    if not name or "\x00" in name or "\\" in name or len(name) > 4096:
        return False
    candidate = PurePosixPath(name)
    return not candidate.is_absolute() and not any(part in {"", ".", ".."} for part in candidate.parts)


def _archive_files(path: Path) -> tuple[dict[str, bytes], int]:
    files: dict[str, bytes] = {}
    total = 0
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_ARCHIVE_FILES:
                raise PackageError("archive_entry_count_invalid")
            names: set[str] = set()
            for entry in entries:
                if entry.filename in names or not _safe_member(entry.filename):
                    raise PackageError("archive_member_unsafe")
                names.add(entry.filename)
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise PackageError("archive_link_forbidden")
                if entry.file_size < 0 or entry.file_size > MAX_MEMBER_BYTES:
                    raise PackageError("archive_member_size_invalid")
                total += entry.file_size
                if total > MAX_TOTAL_BYTES:
                    raise PackageError("archive_expansion_limit_exceeded")
                if not entry.is_dir():
                    files[entry.filename] = archive.read(entry)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise PackageError("archive_invalid") from exc
    return files, len(entries)


def verify_package(package_dir: Path) -> dict[str, object]:
    root = package_dir.resolve()
    try:
        actual_files = {entry.name for entry in root.iterdir() if entry.is_file()}
    except OSError as exc:
        raise PackageError("package_directory_unreadable") from exc
    if actual_files != EXPECTED_FILES:
        raise PackageError("package_file_set_invalid")

    receipt_path = root / "verification.json"
    handoff_path = root / "HANDOFF.md"
    if receipt_path.stat().st_size > MAX_RECEIPT_BYTES or handoff_path.stat().st_size > MAX_HANDOFF_BYTES:
        raise PackageError("metadata_size_invalid")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        handoff = handoff_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageError("metadata_invalid") from exc
    if not isinstance(receipt, dict):
        raise PackageError("receipt_shape_invalid")

    source_commit = receipt.get("sourceCommit")
    source_digest = receipt.get("sourceArchiveSha256")
    deploy_digest = receipt.get("deploymentSha256")
    changed = receipt.get("changedFromGitArchive")
    tests = receipt.get("localExtractedTests")
    if (not isinstance(source_commit, str) or not HEX40.fullmatch(source_commit)
            or not isinstance(source_digest, str) or not HEX64.fullmatch(source_digest)
            or not isinstance(deploy_digest, str) or not HEX64.fullmatch(deploy_digest)
            or not isinstance(changed, list) or not changed
            or any(not isinstance(item, str) or not _safe_member(item) for item in changed)
            or len(set(changed)) != len(changed)
            or not isinstance(tests, dict)):
        raise PackageError("receipt_contract_invalid")
    if receipt.get("serverAcceptance") != "NOT RUN":
        raise PackageError("server_acceptance_claim_invalid")
    if (tests.get("status") != "passed" or tests.get("errors") != 0
            or tests.get("failures") != 0 or not isinstance(tests.get("tests_run"), int)
            or tests["tests_run"] <= 0):
        raise PackageError("local_extracted_tests_not_passed")
    if f"Source commit: `{source_commit[:7]}`" not in handoff:
        raise PackageError("handoff_source_mismatch")

    source_path = root / "workcell-source.zip"
    deploy_path = root / "workcell-deploy.zip"
    if _digest(source_path) != source_digest or _digest(deploy_path) != deploy_digest:
        raise PackageError("archive_digest_mismatch")
    source_files, source_entries = _archive_files(source_path)
    deploy_files, _ = _archive_files(deploy_path)
    if receipt.get("archiveFiles") != source_entries:
        raise PackageError("source_archive_count_mismatch")
    if set(source_files) != set(deploy_files):
        raise PackageError("deployment_file_set_mismatch")
    actual_changed = sorted(name for name in source_files if source_files[name] != deploy_files[name])
    if actual_changed != sorted(changed):
        raise PackageError("deployment_change_set_mismatch")

    return {
        "schema": SCHEMA,
        "status": "passed",
        "source_commit": source_commit,
        "source_archive_sha256": source_digest,
        "deployment_sha256": deploy_digest,
        "source_entries": source_entries,
        "deployment_files": len(deploy_files),
        "changed_files": actual_changed,
        "local_extracted_tests": tests["tests_run"],
        "server_acceptance": "NOT RUN",
        "external_mutation_performed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        report = verify_package(args.package_dir)
    except (OSError, PackageError):
        report = {"schema": SCHEMA, "status": "failed", "external_mutation_performed": False}
        print(json.dumps(report, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
