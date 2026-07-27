from __future__ import annotations

import io
import os
import posixpath
import re
import stat
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


MAX_ARCHIVE_ENTRIES = 256
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 64_000_000
MAX_XML_BYTES = 32_000_000
MAX_SHARED_STRINGS = 100_000
MAX_SHARED_STRING_CHARS = 4_000_000
MAX_PROFILE_COLUMNS = 512
MAX_HEADER_CHARS = 256

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_OFFICE_REL = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
)
_PACKAGE_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_CONTENT_TYPES = "{http://schemas.openxmlformats.org/package/2006/content-types}"
_WORKBOOK_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
)
_WORKSHEET_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
)
_WORKSHEET_RELATIONSHIP = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
)
_CELL_REFERENCE = re.compile(r"\A([A-Za-z]{1,3})([1-9][0-9]*)\Z")
_FORBIDDEN_XML = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


class SpreadsheetError(ValueError):
    pass


@dataclass(frozen=True)
class SpreadsheetRows:
    rows: list[dict[str, object]]
    sheet: str
    truncated: bool
    formula_cells_ignored: int
    error_cells_ignored: int


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        int(getattr(metadata, "st_file_attributes", 0)) & 0x400
        or int(getattr(metadata, "st_reparse_tag", 0))
    )


def _metadata_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        int(getattr(metadata, "st_file_attributes", 0)),
        int(getattr(metadata, "st_reparse_tag", 0)),
    )


def _path_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return _metadata_signature(metadata) + (metadata.st_ctime_ns,)


def _absolute(path: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path(os.path.abspath(os.fspath(candidate)))
    return candidate


def _windows_drive_type(root: str) -> int:
    try:
        import ctypes

        function = ctypes.windll.kernel32.GetDriveTypeW
        function.argtypes = [ctypes.c_wchar_p]
        function.restype = ctypes.c_uint
        return int(function(root))
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise SpreadsheetError("Dataset root drive could not be verified") from exc


def _require_local_absolute(path: Path) -> None:
    if os.name != "nt":
        return
    raw = str(path).replace("/", "\\")
    if raw.startswith(("\\\\", "\\?\\", "\\.\\", "\\??\\")):
        raise SpreadsheetError("Dataset root must be on a local drive")
    if re.fullmatch(r"[A-Za-z]:\\.*", raw) is None:
        raise SpreadsheetError("Dataset root must be an absolute local path")
    if _windows_drive_type(path.anchor) not in {2, 3, 6}:
        raise SpreadsheetError("Dataset root must be on a local drive")


def _validate_path_chain(path: Path, *, final_directory: bool) -> None:
    parts = path.parts
    if not parts:
        raise SpreadsheetError("Dataset path is invalid")
    current = Path(parts[0])
    for index, part in enumerate(parts[1:], start=1):
        current /= part
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise SpreadsheetError("Dataset path is unavailable") from exc
        if _is_link_or_reparse(metadata):
            raise SpreadsheetError("Dataset path contains a link or reparse point")
        is_final = index == len(parts) - 1
        if not is_final or final_directory:
            if not stat.S_ISDIR(metadata.st_mode):
                raise SpreadsheetError("Dataset path is not a directory")
        elif not stat.S_ISREG(metadata.st_mode):
            raise SpreadsheetError("Dataset source is not a regular file")


def read_stable_local_file(
    source: Path,
    *,
    allowed_root: Path | None,
    max_bytes: int,
    require_allowed_root: bool,
) -> tuple[Path, bytes]:
    if require_allowed_root and allowed_root is None:
        raise SpreadsheetError("XLSX datasets require --allow-root")

    candidate = _absolute(source)
    if allowed_root is not None:
        root = _absolute(allowed_root)
        _require_local_absolute(root)
        _validate_path_chain(root, final_directory=True)
        root = root.resolve(strict=True)
        _validate_path_chain(candidate, final_directory=False)
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise SpreadsheetError("Dataset source is outside the allowed root")
    else:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise SpreadsheetError("Dataset source is unavailable") from exc

    try:
        before = os.lstat(resolved)
    except OSError as exc:
        raise SpreadsheetError("Dataset source is unavailable") from exc
    if _is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise SpreadsheetError("Dataset source is not a safe regular file")
    if before.st_size > max_bytes:
        raise SpreadsheetError(f"Dataset exceeds {max_bytes} bytes")

    try:
        with resolved.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if _metadata_signature(opened) != _metadata_signature(before):
                raise SpreadsheetError("Dataset source changed before reading")
            raw = handle.read(max_bytes + 1)
            after = os.fstat(handle.fileno())
        final = os.lstat(resolved)
    except SpreadsheetError:
        raise
    except OSError as exc:
        raise SpreadsheetError("Dataset source could not be read") from exc

    if len(raw) > max_bytes:
        raise SpreadsheetError(f"Dataset exceeds {max_bytes} bytes")
    if not (
        _metadata_signature(before)
        == _metadata_signature(opened)
        == _metadata_signature(after)
        == _metadata_signature(final)
    ) or _path_signature(before) != _path_signature(final):
        raise SpreadsheetError("Dataset source changed while reading")
    return resolved, raw


def _validated_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if not infos or len(infos) > MAX_ARCHIVE_ENTRIES:
        raise SpreadsheetError("XLSX archive entry count is invalid")
    members: dict[str, zipfile.ZipInfo] = {}
    seen: set[str] = set()
    total = 0
    for info in infos:
        name = info.filename
        parts = name.rstrip("/").split("/")
        if (
            not name
            or "\x00" in name
            or "\\" in name
            or name.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
            or ":" in parts[0]
        ):
            raise SpreadsheetError("XLSX archive contains an unsafe path")
        key = name.rstrip("/").casefold()
        if key in seen:
            raise SpreadsheetError("XLSX archive contains duplicate paths")
        seen.add(key)
        if info.flag_bits & 0x1:
            raise SpreadsheetError("Encrypted XLSX archives are unsupported")
        archived_mode = (info.external_attr >> 16) & 0xFFFF
        if archived_mode and stat.S_ISLNK(archived_mode):
            raise SpreadsheetError("XLSX archive contains a linked member")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise SpreadsheetError("XLSX archive compression is unsupported")
        if not info.is_dir():
            if info.file_size < 0 or info.file_size > MAX_XML_BYTES:
                raise SpreadsheetError("XLSX archive member is too large")
            total += info.file_size
            if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise SpreadsheetError("XLSX archive expands beyond the safety limit")
            members[name] = info
    return members


def _read_member(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    name: str,
) -> bytes:
    info = members.get(name)
    if info is None:
        raise SpreadsheetError("XLSX archive is missing a required part")
    try:
        with archive.open(info, "r") as handle:
            data = handle.read(MAX_XML_BYTES + 1)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise SpreadsheetError("XLSX archive member could not be read") from exc
    if len(data) > MAX_XML_BYTES or len(data) != info.file_size:
        raise SpreadsheetError("XLSX archive member size is invalid")
    return data


def _parse_xml(data: bytes) -> ET.Element:
    if _FORBIDDEN_XML.search(data):
        raise SpreadsheetError("XLSX XML declarations are unsafe")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise SpreadsheetError("XLSX XML is malformed") from exc


def _content_types(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
) -> dict[str, str]:
    root = _parse_xml(_read_member(archive, members, "[Content_Types].xml"))
    result: dict[str, str] = {}
    for item in root.findall(f"{_CONTENT_TYPES}Override"):
        part = item.get("PartName", "")
        content_type = item.get("ContentType", "")
        if part and content_type:
            result[part] = content_type
    if result.get("/xl/workbook.xml") != _WORKBOOK_CONTENT_TYPE:
        raise SpreadsheetError("XLSX workbook type is unsupported")
    return result


def _select_sheet(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    requested_sheet: str | None,
) -> tuple[str, str]:
    workbook = _parse_xml(_read_member(archive, members, "xl/workbook.xml"))
    sheets_parent = workbook.find(f"{_MAIN}sheets")
    if sheets_parent is None:
        raise SpreadsheetError("XLSX workbook has no sheets")
    sheets: list[tuple[str, str, str]] = []
    for item in sheets_parent.findall(f"{_MAIN}sheet"):
        name = item.get("name", "").strip()
        relationship_id = item.get(f"{_OFFICE_REL}id", "")
        state = item.get("state", "visible")
        if not name or not relationship_id:
            raise SpreadsheetError("XLSX sheet metadata is invalid")
        sheets.append((name, relationship_id, state))
    if not sheets:
        raise SpreadsheetError("XLSX workbook has no sheets")

    if requested_sheet is None:
        selected = next((item for item in sheets if item[2] == "visible"), None)
        if selected is None:
            raise SpreadsheetError("XLSX workbook has no visible sheet")
    else:
        matches = [item for item in sheets if item[0].casefold() == requested_sheet.casefold()]
        if len(matches) != 1:
            raise SpreadsheetError("Requested XLSX sheet was not found uniquely")
        selected = matches[0]

    relationships = _parse_xml(
        _read_member(archive, members, "xl/_rels/workbook.xml.rels")
    )
    target = None
    for item in relationships.findall(f"{_PACKAGE_REL}Relationship"):
        if item.get("Id") != selected[1]:
            continue
        if (
            item.get("Type") != _WORKSHEET_RELATIONSHIP
            or item.get("TargetMode", "Internal") != "Internal"
        ):
            raise SpreadsheetError("XLSX sheet relationship is unsafe")
        target = item.get("Target", "")
        break
    if not target or target.startswith(("/", "\\")) or "\\" in target:
        raise SpreadsheetError("XLSX sheet relationship is invalid")
    normalized = posixpath.normpath(posixpath.join("xl", target))
    if not normalized.startswith("xl/worksheets/") or normalized not in members:
        raise SpreadsheetError("XLSX sheet target is outside the worksheet area")
    return selected[0], normalized


def _shared_strings(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
) -> list[str]:
    if "xl/sharedStrings.xml" not in members:
        return []
    root = _parse_xml(_read_member(archive, members, "xl/sharedStrings.xml"))
    values: list[str] = []
    total_characters = 0
    for item in root.findall(f"{_MAIN}si"):
        value = "".join(node.text or "" for node in item.iter(f"{_MAIN}t"))
        total_characters += len(value)
        values.append(value)
        if len(values) > MAX_SHARED_STRINGS or total_characters > MAX_SHARED_STRING_CHARS:
            raise SpreadsheetError("XLSX shared strings exceed the safety limit")
    return values


def _column_index(reference: str) -> int:
    match = _CELL_REFERENCE.fullmatch(reference)
    if match is None:
        raise SpreadsheetError("XLSX cell reference is invalid")
    result = 0
    for character in match.group(1).upper():
        result = result * 26 + ord(character) - 64
    result -= 1
    if result >= MAX_PROFILE_COLUMNS:
        raise SpreadsheetError(f"XLSX profile exceeds {MAX_PROFILE_COLUMNS} columns")
    return result


def _cell_value(cell: ET.Element, shared: list[str]) -> tuple[object | None, bool, bool]:
    if cell.find(f"{_MAIN}f") is not None:
        return None, True, False
    cell_type = cell.get("t", "n")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{_MAIN}t")), False, False
    value_node = cell.find(f"{_MAIN}v")
    raw = "" if value_node is None or value_node.text is None else value_node.text
    if cell_type == "s":
        try:
            index = int(raw)
            if index < 0:
                raise ValueError("negative shared string index")
            return shared[index], False, False
        except (ValueError, IndexError) as exc:
            raise SpreadsheetError("XLSX shared string reference is invalid") from exc
    if cell_type == "b":
        if raw not in {"0", "1"}:
            raise SpreadsheetError("XLSX boolean cell is invalid")
        return raw == "1", False, False
    if cell_type == "e":
        return None, False, True
    if cell_type in {"n", "str", "d"}:
        return raw if raw != "" else None, False, False
    raise SpreadsheetError("XLSX cell type is unsupported")


def profile_xlsx(raw: bytes, *, sheet: str | None, max_rows: int) -> SpreadsheetRows:
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw), "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise SpreadsheetError("XLSX archive is invalid") from exc
    with archive:
        members = _validated_members(archive)
        content_types = _content_types(archive, members)
        sheet_name, sheet_path = _select_sheet(archive, members, sheet)
        if content_types.get("/" + sheet_path) != _WORKSHEET_CONTENT_TYPE:
            raise SpreadsheetError("XLSX worksheet type is unsupported")
        shared = _shared_strings(archive, members)
        worksheet = _parse_xml(_read_member(archive, members, sheet_path))

    sheet_data = worksheet.find(f"{_MAIN}sheetData")
    if sheet_data is None:
        raise SpreadsheetError("XLSX worksheet contains no sheet data")

    headers: list[str] | None = None
    rows: list[dict[str, object]] = []
    formula_cells = 0
    error_cells = 0
    truncated = False
    for row_element in sheet_data.findall(f"{_MAIN}row"):
        values: dict[int, object | None] = {}
        meaningful: set[int] = set()
        row_formula_cells = 0
        row_error_cells = 0
        for cell in row_element.findall(f"{_MAIN}c"):
            index = _column_index(cell.get("r", ""))
            if index in values:
                raise SpreadsheetError("XLSX row contains a duplicate cell reference")
            value, is_formula, is_error = _cell_value(cell, shared)
            values[index] = value
            if value is not None or is_formula or is_error:
                meaningful.add(index)
            row_formula_cells += int(is_formula)
            row_error_cells += int(is_error)
        if not meaningful:
            continue

        if headers is None:
            last_column = max(meaningful)
            headers = []
            seen_headers: set[str] = set()
            for index in range(last_column + 1):
                value = values.get(index)
                header = "" if value is None else str(value).strip()
                if (
                    not header
                    or len(header) > MAX_HEADER_CHARS
                    or any(ord(character) < 32 for character in header)
                    or header.casefold() in seen_headers
                ):
                    raise SpreadsheetError("XLSX header row is blank, duplicate, or invalid")
                headers.append(header)
                seen_headers.add(header.casefold())
            continue

        if any(index >= len(headers) for index in meaningful):
            raise SpreadsheetError("XLSX data extends beyond the header row")
        if len(rows) >= max_rows:
            truncated = True
            break
        rows.append({name: values.get(index) for index, name in enumerate(headers)})
        formula_cells += row_formula_cells
        error_cells += row_error_cells

    if headers is None:
        raise SpreadsheetError("XLSX worksheet has no header row")
    if not rows:
        raise SpreadsheetError("XLSX worksheet contains no data rows")
    return SpreadsheetRows(
        rows=rows,
        sheet=sheet_name,
        truncated=truncated,
        formula_cells_ignored=formula_cells,
        error_cells_ignored=error_cells,
    )
