"""Check or atomically refresh the embedded local-company build manifest."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path


MANIFEST_RELATIVE_PATH = Path("src/local_company/build_info.py")
SOURCE_RELATIVE_PATH = Path("src/local_company")
OPERATIONAL_SCRIPT_RELATIVE_PATHS = (
    Path("scripts/check_live_build.py"),
    Path("scripts/check_readiness.py"),
    Path("scripts/check_runtime_supervisor.py"),
    Path("scripts/local_ai.py"),
    Path("scripts/manage_cycle_task.ps1"),
    Path("scripts/run_local_company_prompt.py"),
    Path("scripts/run_scheduled_cycle.py"),
    Path("scripts/runtime_guard.py"),
    Path("scripts/stamp_build_manifest.py"),
)
RELEASE_DIGEST_DOMAIN = b"local-company.release-source.v1\0"
MAX_SOURCE_ENTRIES = 2_048
MAX_SOURCE_FILES = 512
MAX_SOURCE_FILE_BYTES = 4 * 1024 * 1024
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_RELATIVE_PATH_BYTES = 4 * 1024
MAX_SOURCE_DEPTH = 64
MAX_MANIFEST_BYTES = 16 * 1024
MAX_METADATA_BYTES = 64 * 1024
EXPECTED_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID_PATTERN = re.compile(
    r"\Alocal-build-(?P<date>[0-9]{8})\.(?P<revision>[1-9][0-9]{0,3})\Z"
)
LEGACY_MANIFEST_DOCSTRING = """Generated, read-only identity for the local runtime build.

The source digest covers every Python file in this package except this manifest.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""
MANIFEST_DOCSTRING = """Generated, read-only identity for the local runtime build.

The release digest covers every Python file in this package except this manifest,
plus the fixed local lifecycle and orchestration scripts used to operate it.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""


class ManifestError(RuntimeError):
    def __init__(self, message: str, *, replacement_committed: bool = False) -> None:
        super().__init__(message)
        self.replacement_committed = replacement_committed


@dataclass(frozen=True)
class SourceDigest:
    sha256: str
    file_count: int
    total_bytes: int


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if callable(is_junction) and is_junction(path):
        return True
    return bool(getattr(path.lstat(), "st_reparse_tag", 0))


def _validated_layout(project_root: Path) -> tuple[Path, Path, Path]:
    try:
        if _is_link_or_reparse(project_root):
            raise ManifestError("Project root must not be a link or reparse point")
        root_metadata = project_root.stat(follow_symlinks=False)
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise ManifestError("Project root is not a directory")
        root = project_root.resolve(strict=True)
        current = root
        for component in SOURCE_RELATIVE_PATH.parts:
            current /= component
            if _is_link_or_reparse(current):
                raise ManifestError("Project source path contains a link or reparse point")
            metadata = current.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or not current.resolve(strict=True).is_relative_to(root)
            ):
                raise ManifestError("Project source path escaped the project root")
            current = current.resolve(strict=True)
        manifest = root / MANIFEST_RELATIVE_PATH
        if _is_link_or_reparse(manifest):
            raise ManifestError("Build manifest must not be a link or reparse point")
        if not manifest.resolve(strict=True).is_relative_to(root):
            raise ManifestError("Build manifest escaped the project root")
        return root, current, manifest
    except ManifestError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ManifestError(f"Could not validate the project layout: {exc}") from exc


def _collect_package_sources(source_root: Path) -> tuple[Path, list[Path]]:
    if _is_link_or_reparse(source_root):
        raise ManifestError("Operational source root must not be a link or reparse point")
    root = source_root.resolve(strict=True)
    if not source_root.is_dir():
        raise ManifestError("Operational source root is not a directory")
    files: list[Path] = []
    entry_count = 0

    def collect(directory: Path, depth: int) -> None:
        nonlocal entry_count
        if depth > MAX_SOURCE_DEPTH:
            raise ManifestError("Operational source tree exceeds the depth limit")
        with os.scandir(directory) as entries:
            for entry in entries:
                entry_count += 1
                if entry_count > MAX_SOURCE_ENTRIES:
                    raise ManifestError("Operational source tree exceeds the entry limit")
                path = Path(entry.path)
                if _is_link_or_reparse(path):
                    raise ManifestError(
                        "Operational source tree contains a link or reparse point"
                    )
                if entry.is_dir(follow_symlinks=False):
                    collect(path, depth + 1)
                elif entry.is_file(follow_symlinks=False):
                    relative = path.relative_to(root)
                    if (
                        path.suffix.casefold() == ".py"
                        and relative.as_posix() != "build_info.py"
                    ):
                        if len(relative.as_posix().encode("utf-8")) > MAX_RELATIVE_PATH_BYTES:
                            raise ManifestError(
                                "Operational source path exceeds its byte limit"
                            )
                        files.append(path)
                        if len(files) > MAX_SOURCE_FILES:
                            raise ManifestError(
                                "Operational source tree exceeds the file limit"
                            )
                elif path.suffix.casefold() == ".py":
                    raise ManifestError("Operational Python source is not a regular file")

    collect(root, 0)
    if not files:
        raise ManifestError("Operational source tree contains no Python files")
    if not any(path.relative_to(root).as_posix() == "__init__.py" for path in files):
        raise ManifestError("Operational source tree is missing __init__.py")
    return root, files


def _calculate_framed_digest(
    root: Path, files: list[Path], *, domain: bytes = b"",
) -> SourceDigest:
    if not files or len(files) > MAX_SOURCE_FILES or len(set(files)) != len(files):
        raise ManifestError("Operational source file set is invalid")
    if not isinstance(domain, bytes) or len(domain) > 256:
        raise ManifestError("Operational source digest domain is invalid")
    files.sort(key=lambda path: path.relative_to(root).as_posix().encode("utf-8"))
    digest = hashlib.sha256()
    digest.update(domain)
    total_bytes = 0
    for path in files:
        relative_text = path.relative_to(root).as_posix()
        relative = relative_text.encode("utf-8")
        if len(relative) > MAX_RELATIVE_PATH_BYTES:
            raise ManifestError("Operational source path exceeds its byte limit")
        if _is_link_or_reparse(path) or not path.resolve(strict=True).is_relative_to(root):
            raise ManifestError("Operational source escaped its root")
        before = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SOURCE_FILE_BYTES:
            raise ManifestError("Operational source file exceeds its size limit")
        if total_bytes + before.st_size > MAX_SOURCE_BYTES:
            raise ManifestError("Operational source tree exceeds its byte limit")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            content = handle.read(MAX_SOURCE_FILE_BYTES + 1)
            after = os.fstat(handle.fileno())
        current = path.stat(follow_symlinks=False)
        signatures = {
            (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
            for item in (before, opened, after, current)
        }
        if (
            len(signatures) != 1
            or not stat.S_ISREG(opened.st_mode)
            or len(content) != opened.st_size
            or len(content) > MAX_SOURCE_FILE_BYTES
            or not path.resolve(strict=True).is_relative_to(root)
        ):
            raise ManifestError("Operational source changed while it was hashed")
        total_bytes += len(content)
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return SourceDigest(digest.hexdigest(), len(files), total_bytes)


def _validated_operational_scripts(root: Path) -> list[Path]:
    if len(set(OPERATIONAL_SCRIPT_RELATIVE_PATHS)) != len(OPERATIONAL_SCRIPT_RELATIVE_PATHS):
        raise ManifestError("Operational lifecycle script allowlist contains duplicates")
    scripts_root = root / "scripts"
    if _is_link_or_reparse(scripts_root):
        raise ManifestError("Operational scripts path must not be a link or reparse point")
    scripts_metadata = scripts_root.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(scripts_metadata.st_mode)
        or not scripts_root.resolve(strict=True).is_relative_to(root)
    ):
        raise ManifestError("Operational scripts path escaped the project root")
    files = []
    for relative in OPERATIONAL_SCRIPT_RELATIVE_PATHS:
        path = root / relative
        if path.parent != scripts_root or _is_link_or_reparse(path):
            raise ManifestError("Operational lifecycle script path is unsafe")
        metadata = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not path.resolve(strict=True).is_relative_to(root)
        ):
            raise ManifestError("Operational lifecycle script is not a regular project file")
        files.append(path)
    return files


def calculate_source_digest(source_root: Path) -> SourceDigest:
    try:
        root, files = _collect_package_sources(source_root)
        return _calculate_framed_digest(root, files)
    except ManifestError:
        raise
    except (OSError, OverflowError, RuntimeError, UnicodeError, ValueError) as exc:
        raise ManifestError(f"Could not hash operational source: {exc}") from exc


def calculate_release_digest(project_root: Path) -> SourceDigest:
    try:
        root, source_root, _ = _validated_layout(Path(project_root))
        _, package_files = _collect_package_sources(source_root)
        scripts_root = root / "scripts"
        scripts_before = scripts_root.stat(follow_symlinks=False)
        lifecycle_files = _validated_operational_scripts(root)
        digest = _calculate_framed_digest(
            root, package_files + lifecycle_files, domain=RELEASE_DIGEST_DOMAIN,
        )
        scripts_after = scripts_root.stat(follow_symlinks=False)
        if (
            _is_link_or_reparse(scripts_root)
            or (scripts_before.st_dev, scripts_before.st_ino, scripts_before.st_mode)
            != (scripts_after.st_dev, scripts_after.st_ino, scripts_after.st_mode)
        ):
            raise ManifestError("Operational scripts path changed while it was hashed")
        return digest
    except ManifestError:
        raise
    except (OSError, OverflowError, RuntimeError, UnicodeError, ValueError) as exc:
        raise ManifestError(f"Could not hash release source: {exc}") from exc


def _read_bounded_text(path: Path, maximum: int, label: str) -> str:
    try:
        if _is_link_or_reparse(path.parent) or _is_link_or_reparse(path):
            raise ManifestError(f"{label} must not be a link or reparse point")
        before = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ManifestError(f"{label} is not a bounded regular file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            content = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
        current = path.stat(follow_symlinks=False)
        signatures = {
            (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
            for item in (before, opened, after, current)
        }
        if (
            len(signatures) != 1
            or not stat.S_ISREG(opened.st_mode)
            or len(content) != opened.st_size
            or len(content) > maximum
            or content.startswith(b"\xef\xbb\xbf")
            or b"\x00" in content
        ):
            raise ManifestError(f"{label} changed, is oversized, or has unsafe encoding")
        return content.decode("utf-8", errors="strict")
    except ManifestError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise ManifestError(f"Could not read {label}: {exc}") from exc


def parse_build_id(build_id: str) -> tuple[date, int]:
    match = BUILD_ID_PATTERN.fullmatch(build_id)
    if match is None:
        raise ManifestError("Build ID must match local-build-YYYYMMDD.N with N from 1 to 9999")
    raw_date = match.group("date")
    try:
        build_date = date.fromisoformat(
            f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
        )
    except ValueError as exc:
        raise ManifestError("Build ID contains an invalid calendar date") from exc
    return build_date, int(match.group("revision"))


def _manifest_values(
    path: Path, *, allow_legacy_docstring: bool = False,
) -> tuple[str, str, str, str, bool]:
    text = _read_bounded_text(path, MAX_MANIFEST_BYTES, "Build manifest")
    try:
        tree = ast.parse(text, filename="build_info.py", mode="exec")
    except SyntaxError as exc:
        raise ManifestError("Build manifest is not valid inert Python") from exc
    if len(tree.body) != 4:
        raise ManifestError("Build manifest must contain only its docstring and three assignments")
    docstring = tree.body[0]
    if not (
        isinstance(docstring, ast.Expr)
        and isinstance(docstring.value, ast.Constant)
        and type(docstring.value.value) is str
    ):
        raise ManifestError("Build manifest docstring is missing or unexpected")
    if docstring.value.value == MANIFEST_DOCSTRING:
        legacy_docstring = False
    elif allow_legacy_docstring and docstring.value.value == LEGACY_MANIFEST_DOCSTRING:
        legacy_docstring = True
    elif docstring.value.value == LEGACY_MANIFEST_DOCSTRING:
        raise ManifestError("Build manifest digest coverage requires migration")
    else:
        raise ManifestError("Build manifest docstring is missing or unexpected")
    values: dict[str, str] = {}
    expected_names = ("RUNTIME_BUILD_SCHEMA", "BUILD_ID", "SOURCE_SHA256")
    for node, expected_name in zip(tree.body[1:], expected_names, strict=True):
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == expected_name
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) is str
        ):
            raise ManifestError("Build manifest contains a noncanonical or executable statement")
        values[expected_name] = node.value.value
    schema = values["RUNTIME_BUILD_SCHEMA"]
    build_id = values["BUILD_ID"]
    source_hash = values["SOURCE_SHA256"]
    if schema != EXPECTED_SCHEMA:
        raise ManifestError("Embedded runtime build schema is unsupported")
    parse_build_id(build_id)
    if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise ManifestError("Embedded SOURCE_SHA256 is malformed")
    return text, schema, build_id, source_hash, legacy_docstring


def _package_version(project_root: Path) -> str:
    metadata_text = _read_bounded_text(
        project_root / "pyproject.toml", MAX_METADATA_BYTES, "Project metadata",
    )
    try:
        metadata = tomllib.loads(metadata_text)
        project_version = metadata["project"]["version"]
    except (KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError("Project metadata has no valid package version") from exc
    init_text = _read_bounded_text(
        project_root / SOURCE_RELATIVE_PATH / "__init__.py",
        MAX_SOURCE_FILE_BYTES,
        "Package initializer",
    )
    try:
        init_tree = ast.parse(init_text, filename="__init__.py", mode="exec")
    except SyntaxError as exc:
        raise ManifestError("Package initializer is not valid Python") from exc
    versions = []
    for node in init_tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__version__"
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) is str
        ):
            versions.append(node.value.value)
    if len(versions) != 1 or type(project_version) is not str:
        raise ManifestError("Package initializer must contain one literal __version__")
    if versions[0] != project_version:
        raise ManifestError("pyproject.toml version does not match package __version__")
    return project_version


def _render_manifest(build_id: str, source_hash: str) -> str:
    return (
        f'"""{MANIFEST_DOCSTRING}"""\n\n'
        f'RUNTIME_BUILD_SCHEMA = "{EXPECTED_SCHEMA}"\n'
        f'BUILD_ID = "{build_id}"\n'
        f'SOURCE_SHA256 = "{source_hash}"\n'
    )


def check_project(project_root: Path) -> dict[str, object]:
    root, _, manifest_path = _validated_layout(project_root)
    digest = calculate_release_digest(root)
    package_version = _package_version(root)
    _, schema, build_id, embedded_hash, _ = _manifest_values(manifest_path)
    if embedded_hash != digest.sha256:
        raise ManifestError(
            f"Build manifest is stale: embedded {embedded_hash}, calculated {digest.sha256}"
        )
    return {
        "status": "ok",
        "mode": "check",
        "changed": False,
        "schema": schema,
        "package_version": package_version,
        "build_id": build_id,
        "source_sha256": digest.sha256,
        "file_count": digest.file_count,
        "source_bytes": digest.total_bytes,
    }


def _atomic_write(path: Path, content: str) -> None:
    temporary: Path | None = None
    replacement_committed = False
    try:
        if _is_link_or_reparse(path.parent) or _is_link_or_reparse(path):
            raise ManifestError("Build manifest path became unsafe before replacement")
        metadata = path.stat(follow_symlinks=False)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(metadata.st_mode))
        current = path.stat(follow_symlinks=False)
        if (
            _is_link_or_reparse(path)
            or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
            != (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        ):
            raise ManifestError("Build manifest changed before atomic replacement")
        os.replace(temporary, path)
        replacement_committed = True
        if os.name != "nt":
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_descriptor = os.open(path.parent, directory_flags)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except ManifestError as exc:
        if replacement_committed and not exc.replacement_committed:
            raise ManifestError(
                str(exc), replacement_committed=True,
            ) from exc
        raise
    except (OSError, UnicodeError) as exc:
        raise ManifestError(
            f"Could not atomically update build manifest: {exc}",
            replacement_committed=replacement_committed,
        ) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def stamp_project(project_root: Path, build_id: str) -> dict[str, object]:
    requested_order = parse_build_id(build_id)
    root, source_root, manifest_path = _validated_layout(project_root)
    digest = calculate_release_digest(root)
    package_version = _package_version(root)
    text, schema, previous_build_id, previous_hash, legacy_docstring = _manifest_values(
        manifest_path, allow_legacy_docstring=True,
    )
    previous_order = parse_build_id(previous_build_id)
    if requested_order < previous_order:
        raise ManifestError("Build ID must not move backward")
    if legacy_docstring:
        legacy_digest = calculate_source_digest(source_root)
        if legacy_digest.sha256 != previous_hash:
            raise ManifestError("Legacy build manifest is stale")
        if requested_order <= previous_order:
            raise ManifestError("Build ID must advance when digest coverage changes")
    elif digest.sha256 != previous_hash and build_id == previous_build_id:
        raise ManifestError("Build ID must change when operational source changes")
    confirmation = calculate_release_digest(root)
    if confirmation != digest:
        raise ManifestError("Operational source changed between release scans")
    updated = _render_manifest(build_id, digest.sha256)
    if updated != text:
        _atomic_write(manifest_path, updated)
    try:
        verified = check_project(root)
    except ManifestError as exc:
        raise ManifestError(
            f"Post-update verification failed; run --check before continuing: {exc}",
            replacement_committed=updated != text,
        ) from exc
    if verified["build_id"] != build_id or verified["source_sha256"] != digest.sha256:
        raise ManifestError(
            "Build manifest verification failed after the atomic update",
            replacement_committed=updated != text,
        )
    return {
        "status": "ok",
        "mode": "write",
        "changed": updated != text,
        "schema": schema,
        "package_version": package_version,
        "build_id": build_id,
        "previous_build_id": previous_build_id,
        "source_sha256": digest.sha256,
        "file_count": digest.file_count,
        "source_bytes": digest.total_bytes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="verify without writing")
    mode.add_argument("--write", action="store_true", help="atomically refresh the manifest")
    parser.add_argument("--build-id", help="required with --write: local-build-YYYYMMDD.N")
    args = parser.parse_args(argv)
    if args.write and not args.build_id:
        parser.error("--write requires --build-id")
    if args.check and args.build_id:
        parser.error("--build-id is valid only with --write")
    if args.write:
        try:
            parse_build_id(args.build_id)
        except ManifestError as exc:
            parser.error(str(exc))
    try:
        project_root = Path(__file__).resolve().parents[1]
        result = (
            stamp_project(project_root, args.build_id)
            if args.write
            else check_project(project_root)
        )
    except ManifestError as exc:
        error = {
            "status": "error",
            "mode": "write" if args.write else "check",
            "changed": exc.replacement_committed,
            "replacement_committed": exc.replacement_committed,
            "error": str(exc),
        }
        if exc.replacement_committed:
            error["recovery"] = "Run --check before continuing; do not restart the service yet."
        print(json.dumps(error, sort_keys=True), file=sys.stderr)
        return 4 if exc.replacement_committed else 1
    except OSError as exc:
        print(json.dumps({
            "status": "error",
            "mode": "write" if args.write else "check",
            "changed": False,
            "replacement_committed": False,
            "error": f"Local filesystem operation failed: {exc}",
        }, sort_keys=True), file=sys.stderr)
        return 3
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
