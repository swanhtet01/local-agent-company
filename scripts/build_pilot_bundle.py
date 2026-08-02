from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
import zipfile
from pathlib import Path

try:
    from .verify_pilot_bundle import BUNDLE_SCHEMA, MANIFEST_NAME, PREREQUISITES, verify_pilot_bundle
except ImportError:
    from verify_pilot_bundle import BUNDLE_SCHEMA, MANIFEST_NAME, PREREQUISITES, verify_pilot_bundle


ROOT_FILES = (
    "PRODUCT.md",
    "company-mcp.cmd", "local-ai-menu.cmd", "local-ai.cmd", "local-code.cmd",
    "local-company-agent.cmd", "local-company.cmd", "setup-local-ai.cmd",
    "pyproject.toml",
)
SOURCE_PATTERNS = (
    ("src/local_company", "*.py"),
    ("scripts", "*.py"),
    ("scripts", "*.ps1"),
    ("tests", "*.py"),
)
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
MAX_SOURCE_FILE_BYTES = 4 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024


class BundleBuildError(ValueError):
    pass


def _personal_path_markers() -> tuple[bytes, ...]:
    candidates: list[str | Path | None] = [
        Path.home(),
        os.getenv("USERPROFILE"),
        os.getenv("ONEDRIVE"),
        os.getenv("OneDriveConsumer"),
        os.getenv("OneDriveCommercial"),
    ]
    markers = {b"onedrive" + b" - bda"}
    for candidate in candidates:
        if candidate is None:
            continue
        value = str(candidate).strip().rstrip("\\/").lower()
        if not value:
            continue
        markers.add(value.encode("utf-8"))
        markers.add(value.replace("\\", "/").encode("utf-8"))
        markers.add(value.replace("/", "\\").encode("utf-8"))
    return tuple(sorted(markers))


def _validated_source_payload(name: str, payload: bytes) -> tuple[str, bytes]:
    lowered = payload.lower()
    if any(marker in lowered for marker in _personal_path_markers()):
        raise BundleBuildError(f"bundle_source_contains_personal_path:{name}")
    return name, payload


def _selected_files(root: Path) -> list[Path]:
    values = [root / name for name in ROOT_FILES]
    for directory, pattern in SOURCE_PATTERNS:
        values.extend((root / directory).glob(pattern))
    files = sorted(set(values), key=lambda path: path.relative_to(root).as_posix())
    if not files or any(not path.exists() for path in files):
        raise BundleBuildError("bundle_required_source_missing")
    total = 0
    for path in files:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise BundleBuildError("bundle_source_unsafe")
        if metadata.st_size > MAX_SOURCE_FILE_BYTES:
            raise BundleBuildError("bundle_source_too_large")
        total += metadata.st_size
    if total > MAX_SOURCE_BYTES:
        raise BundleBuildError("bundle_sources_too_large")
    return files


def _bundle_readme() -> bytes:
    return (
        "SuperMega Local AI - private pilot bundle\r\n"
        "\r\n"
        "Status: owner-review evaluation artifact. It is not published, signed,\r\n"
        "licensed for redistribution, or proven production-ready.\r\n"
        "\r\n"
        "Requirements:\r\n"
        "- Windows 11 and Python 3.11 or newer\r\n"
        "- Ollama installed separately, bound to 127.0.0.1\r\n"
        "- qwen3.5:0.8b for bootstrap use; a measured larger model is optional\r\n"
        "- qwen2.5-coder:0.5b for the read-only low-memory ask command\r\n"
        "- OpenCode installed separately for repository coding work\r\n"
        "\r\n"
        "After extraction, run setup-local-ai.cmd --preview, then --apply.\r\n"
        "Run setup-local-ai.cmd --check after completing any reported actions.\r\n"
        "The check includes a model-free stdio handshake with the bundled company MCP.\r\n"
        "Use local-ai.cmd ask \"QUESTION\" for a grounded private draft.\r\n"
        "Keep model servers loopback-only and review all consequential actions.\r\n"
        "Use scripts\\verify_pilot_bundle.py against the original ZIP before use.\r\n"
    ).encode("utf-8")


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def build_pilot_bundle(project_root: Path, output_directory: Path) -> dict[str, object]:
    root = project_root.resolve(strict=True)
    output = output_directory.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if output.is_symlink() or not output.is_dir():
        raise BundleBuildError("bundle_output_directory_unsafe")

    source_payloads: list[tuple[str, bytes]] = []
    for path in _selected_files(root):
        source_payloads.append(_validated_source_payload(
            path.relative_to(root).as_posix(), path.read_bytes(),
        ))
    source_payloads.append(("BUNDLE-README.txt", _bundle_readme()))
    source_payloads.sort(key=lambda item: item[0])

    source_root = str(root / "src")
    if source_root not in sys.path:
        sys.path.insert(0, source_root)
    from local_company import __version__
    from local_company.build_info import BUILD_ID, SOURCE_SHA256

    file_records = [
        {"path": name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        for name, payload in source_payloads
    ]
    identity = {
        "schema": BUNDLE_SCHEMA,
        "distributionStatus": "private_owner_review",
        "licenseStatus": "no_license_grant",
        "build": {
            "packageVersion": __version__, "buildId": BUILD_ID,
            "runtimeSourceSha256": SOURCE_SHA256,
        },
        "prerequisites": PREREQUISITES,
        "files": file_records,
    }
    framed = json.dumps(identity, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    bundle_id = hashlib.sha256(b"supermega.local-ai-pilot-bundle.v1\0" + framed).hexdigest()[:12]
    manifest = {"schema": BUNDLE_SCHEMA, "bundleId": bundle_id, **{key: value for key, value in identity.items() if key != "schema"}}
    manifest_bytes = (json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("ascii")
    archive_name = f"supermega-local-ai-pilot-{BUILD_ID}-{bundle_id}.zip"
    target = output / archive_name

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b", dir=output, prefix=".pilot-bundle-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
            for name, payload in source_payloads:
                bundle.writestr(_zip_info(name), payload, compresslevel=9)
            bundle.writestr(_zip_info(MANIFEST_NAME), manifest_bytes, compresslevel=9)
        candidate = temporary.read_bytes()
        digest = hashlib.sha256(candidate).hexdigest()
        hash_path = output / f"{archive_name}.sha256"
        hash_bytes = f"{digest}  {archive_name}\n".encode("ascii")
        if hash_path.exists() and (
            hash_path.is_symlink() or hash_path.read_bytes() != hash_bytes
        ):
            raise BundleBuildError("bundle_hash_collision")
        created = False
        if target.exists():
            if target.is_symlink() or target.read_bytes() != candidate:
                raise BundleBuildError("bundle_output_collision")
        else:
            os.replace(temporary, target)
            temporary = None
            created = True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    verified = verify_pilot_bundle(target)
    if verified["bundleSha256"] != digest:
        raise BundleBuildError("bundle_post_write_integrity_invalid")
    if hash_path.exists():
        pass
    else:
        hash_temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=output, prefix=".pilot-hash-", suffix=".tmp", delete=False,
            ) as stream:
                hash_temporary = Path(stream.name)
                stream.write(hash_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(hash_temporary, hash_path)
            hash_temporary = None
        finally:
            if hash_temporary is not None:
                hash_temporary.unlink(missing_ok=True)
    return {
        "schema": "supermega.local-ai-pilot-bundle-build.v1",
        "status": "built", "bundleId": bundle_id, "bundlePath": str(target),
        "bundleSha256": digest, "hashPath": str(hash_path),
        "fileCount": verified["fileCount"], "sourceBytes": verified["sourceBytes"],
        "archiveBytes": target.stat().st_size, "created": created,
        "distributionStatus": "private_owner_review", "licenseStatus": "no_license_grant",
        "privateStateIncluded": False, "credentialsIncluded": False,
        "modelWeightsIncluded": False, "externalPublicationAuthorized": False,
        "externalActionPerformed": False, "modelCalled": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a deterministic private SuperMega Local AI pilot ZIP")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve(strict=True).parents[1]
    try:
        result = build_pilot_bundle(root, args.output_dir)
    except (OSError, BundleBuildError, ValueError) as error:
        print(json.dumps({
            "schema": "supermega.local-ai-pilot-bundle-build.v1",
            "status": "error", "reason": str(error),
            "externalActionPerformed": False, "modelCalled": False,
        }, separators=(",", ":"), sort_keys=True))
        return 1
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
