from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import zipfile
import zlib
from pathlib import Path, PurePosixPath


BUNDLE_SCHEMA = "supermega.local-ai-pilot-bundle.v1"
MANIFEST_NAME = "BUNDLE-MANIFEST.json"
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 512
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
PREREQUISITES = [
    "Windows 11", "Python 3.11+", "Ollama loopback runtime",
    "downloaded local model", "OpenCode for coding-agent use",
]


class BundleVerificationError(ValueError):
    pass


def _safe_member_name(name: str) -> bool:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def verify_pilot_bundle(archive_path: Path) -> dict[str, object]:
    archive = archive_path.resolve(strict=True)
    metadata = archive.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise BundleVerificationError("bundle_file_unsafe")
    if not 1 <= metadata.st_size <= MAX_ARCHIVE_BYTES:
        raise BundleVerificationError("bundle_size_invalid")

    try:
        with zipfile.ZipFile(archive, "r") as bundle:
            infos = bundle.infolist()
            names = [item.filename for item in infos]
            if not 1 <= len(infos) <= MAX_MEMBERS or len(names) != len(set(names)):
                raise BundleVerificationError("bundle_members_invalid")
            if any(
                item.is_dir() or not _safe_member_name(item.filename)
                or item.file_size > MAX_MEMBER_BYTES
                or item.compress_size > MAX_MEMBER_BYTES
                or (
                    (item.external_attr >> 16) != 0
                    and not stat.S_ISREG(item.external_attr >> 16)
                )
                for item in infos
            ):
                raise BundleVerificationError("bundle_member_unsafe")
            if sum(item.file_size for item in infos) > MAX_TOTAL_BYTES:
                raise BundleVerificationError("bundle_expansion_limit_exceeded")
            if names.count(MANIFEST_NAME) != 1:
                raise BundleVerificationError("bundle_manifest_missing")
            manifest_bytes = bundle.read(MANIFEST_NAME)
            if len(manifest_bytes) > MAX_MEMBER_BYTES:
                raise BundleVerificationError("bundle_manifest_invalid")
            try:
                manifest = json.loads(manifest_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise BundleVerificationError("bundle_manifest_invalid") from error
            if not isinstance(manifest, dict) or set(manifest) != {
                "schema", "bundleId", "distributionStatus", "licenseStatus",
                "build", "prerequisites", "files",
            }:
                raise BundleVerificationError("bundle_manifest_invalid")
            if (
                manifest.get("schema") != BUNDLE_SCHEMA
                or not isinstance(manifest.get("bundleId"), str)
                or re.fullmatch(r"[0-9a-f]{12}", manifest["bundleId"]) is None
                or manifest.get("distributionStatus") != "private_owner_review"
                or manifest.get("licenseStatus") != "no_license_grant"
                or not isinstance(manifest.get("build"), dict)
                or manifest.get("prerequisites") != PREREQUISITES
                or not isinstance(manifest.get("files"), list)
            ):
                raise BundleVerificationError("bundle_manifest_invalid")
            build = manifest["build"]
            if (
                set(build) != {"packageVersion", "buildId", "runtimeSourceSha256"}
                or not isinstance(build.get("packageVersion"), str)
                or re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.!+_-]{0,127}", build["packageVersion"]) is None
                or not isinstance(build.get("buildId"), str)
                or re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.!+_-]{0,127}", build["buildId"]) is None
                or not isinstance(build.get("runtimeSourceSha256"), str)
                or SHA256_PATTERN.fullmatch(build["runtimeSourceSha256"]) is None
            ):
                raise BundleVerificationError("bundle_build_identity_invalid")
            files = manifest["files"]
            if not 1 <= len(files) < MAX_MEMBERS:
                raise BundleVerificationError("bundle_manifest_files_invalid")
            expected_names: list[str] = []
            total = 0
            for item in files:
                if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
                    raise BundleVerificationError("bundle_manifest_files_invalid")
                name = item.get("path")
                size = item.get("bytes")
                digest = item.get("sha256")
                if (
                    not isinstance(name, str) or not _safe_member_name(name)
                    or name == MANIFEST_NAME
                    or type(size) is not int or not 0 <= size <= MAX_MEMBER_BYTES
                    or not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None
                ):
                    raise BundleVerificationError("bundle_manifest_files_invalid")
                try:
                    payload = bundle.read(name)
                except KeyError as error:
                    raise BundleVerificationError("bundle_member_missing") from error
                if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
                    raise BundleVerificationError("bundle_member_integrity_invalid")
                expected_names.append(name)
                total += size
            if expected_names != sorted(expected_names) or len(expected_names) != len(set(expected_names)):
                raise BundleVerificationError("bundle_manifest_order_invalid")
            if set(names) != set(expected_names) | {MANIFEST_NAME}:
                raise BundleVerificationError("bundle_unmanifested_member")
            identity = {
                key: manifest[key] for key in (
                    "schema", "distributionStatus", "licenseStatus", "build",
                    "prerequisites", "files",
                )
            }
            framed = json.dumps(
                identity, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
            ).encode("ascii")
            expected_bundle_id = hashlib.sha256(
                b"supermega.local-ai-pilot-bundle.v1\0" + framed,
            ).hexdigest()[:12]
            if manifest["bundleId"] != expected_bundle_id:
                raise BundleVerificationError("bundle_identity_invalid")
    except (OSError, zipfile.BadZipFile, RuntimeError, zlib.error) as error:
        if isinstance(error, BundleVerificationError):
            raise
        raise BundleVerificationError("bundle_archive_invalid") from error

    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    return {
        "schema": "supermega.local-ai-pilot-bundle-verification.v1",
        "status": "verified", "bundleId": manifest["bundleId"],
        "bundleSha256": archive_sha256, "fileCount": len(files),
        "sourceBytes": total, "externalActionPerformed": False,
        "modelCalled": False, "archiveExtracted": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a SuperMega Local AI pilot ZIP without extracting it")
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    try:
        result = verify_pilot_bundle(args.archive)
    except (OSError, BundleVerificationError) as error:
        print(json.dumps({
            "schema": "supermega.local-ai-pilot-bundle-verification.v1",
            "status": "invalid", "reason": str(error),
            "externalActionPerformed": False, "modelCalled": False,
            "archiveExtracted": False,
        }, separators=(",", ":"), sort_keys=True))
        return 1
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
