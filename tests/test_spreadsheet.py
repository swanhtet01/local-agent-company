import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from local_company.cli import parser
from local_company.core import Company, MockModel


MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"


def write_test_workbook(path: Path) -> None:
    content_types = f"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="{CONTENT_TYPES}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""
    workbook = f"""<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="{MAIN}" xmlns:r="{OFFICE_REL}">
  <sheets><sheet name="Sales" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    relationships = f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{PACKAGE_REL}">
  <Relationship Id="rId1" Type="{OFFICE_REL}/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    shared_strings = f"""<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="{MAIN}" count="5" uniqueCount="5">
  <si><t>name</t></si><si><t>amount</t></si><si><t>note</t></si>
  <si><t>alpha</t></si><si><t>beta</t></si>
</sst>"""
    worksheet = f"""<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="{MAIN}"><sheetData>
  <row r="1">
    <c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c>
  </row>
  <row r="2">
    <c r="A2" t="s"><v>3</v></c><c r="B2"><v>10</v></c><c r="C2" t="inlineStr"><is><t>ok</t></is></c>
  </row>
  <row r="3">
    <c r="A3" t="s"><v>4</v></c><c r="B3"><f>1+2</f><v>3</v></c><c r="C3" t="e"><v>#DIV/0!</v></c>
  </row>
  <row r="4">
    <c r="A4" t="s"><v>3</v></c><c r="B4"><v>10</v></c><c r="C4" t="inlineStr"><is><t>ok</t></is></c>
  </row>
</sheetData></worksheet>"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/sharedStrings.xml", shared_strings)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)


class SpreadsheetDatasetTests(unittest.TestCase):
    def test_cli_exposes_explicit_xlsx_root_and_sheet_controls(self):
        args = parser().parse_args(
            [
                "datasets",
                "add",
                "sales.xlsx",
                "--project",
                "Spreadsheet Lab",
                "--allow-root",
                "approved",
                "--sheet",
                "Sales",
                "--key",
                "name",
            ]
        )
        self.assertEqual(args.allow_root, Path("approved"))
        self.assertEqual(args.sheet, "Sales")
        self.assertEqual(args.key_columns, ["name"])

    def test_xlsx_profile_is_allowlisted_read_only_and_formula_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            approved = root / "approved"
            approved.mkdir()
            source = approved / "sales.xlsx"
            write_test_workbook(source)
            original = source.read_bytes()

            company = Company(root / "state", MockModel())
            company.create_project("Spreadsheet Lab")
            dataset_id, brief, profile = company.profile_dataset(
                source,
                "Spreadsheet Lab",
                allowed_root=approved,
                sheet="sales",
                key_columns=["name"],
            )

            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(profile["format"], "xlsx")
            self.assertEqual(profile["sheet"], "Sales")
            self.assertEqual(profile["profiled_rows"], 3)
            self.assertEqual(profile["quality_flags"]["duplicate_rows"], 1)
            self.assertEqual(profile["quality_flags"]["formula_cells_ignored"], 1)
            self.assertEqual(profile["quality_flags"]["error_cells_ignored"], 1)
            self.assertEqual(profile["columns"]["amount"]["missing"], 1)
            self.assertEqual(profile["columns"]["amount"]["missing_rate"], 0.333333)
            self.assertEqual(profile["columns"]["amount"]["types"], {"integer": 2, "missing": 1})
            self.assertEqual(profile["columns"]["amount"]["numeric"]["mean"], 10)
            self.assertEqual(profile["key_check"]["duplicate_rows"], 2)
            self.assertEqual(profile["key_check"]["uniqueness_rate"], 0.666667)
            brief_text = brief.read_text(encoding="utf-8")
            self.assertIn("Sheet: `Sales`", brief_text)
            self.assertIn("Formula cells ignored: 1", brief_text)
            self.assertIn("Rows affected by duplicate keys: 2", brief_text)
            self.assertNotIn("alpha", brief_text)
            self.assertNotIn("1+2", brief_text)
            self.assertEqual(company.dataset_detail(dataset_id)["profile"]["sheet"], "Sales")

    def test_xlsx_requires_allow_root_and_rejects_escape_or_unsafe_zip_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            approved = root / "approved"
            other = root / "other"
            approved.mkdir()
            other.mkdir()
            source = approved / "sales.xlsx"
            write_test_workbook(source)
            company = Company(root / "state", MockModel())
            company.create_project("Guarded Data")

            with self.assertRaisesRegex(ValueError, "require --allow-root"):
                company.profile_dataset(source, "Guarded Data")
            with self.assertRaisesRegex(ValueError, "outside the allowed root"):
                company.profile_dataset(source, "Guarded Data", allowed_root=other)
            with self.assertRaisesRegex(ValueError, "not found uniquely"):
                company.profile_dataset(
                    source, "Guarded Data", allowed_root=approved, sheet="Missing"
                )
            with self.assertRaisesRegex(ValueError, "Unknown dataset key column"):
                company.profile_dataset(
                    source,
                    "Guarded Data",
                    allowed_root=approved,
                    key_columns=["missing_key"],
                )
            with self.assertRaisesRegex(ValueError, "unique non-empty names"):
                company.profile_dataset(
                    source,
                    "Guarded Data",
                    allowed_root=approved,
                    key_columns=["name", "Name"],
                )

            unsafe = approved / "unsafe.xlsx"
            with zipfile.ZipFile(unsafe, "w") as archive:
                archive.writestr("../escape.xml", "unsafe")
            with self.assertRaisesRegex(ValueError, "unsafe path"):
                company.profile_dataset(unsafe, "Guarded Data", allowed_root=approved)

            linked_member = approved / "linked-member.xlsx"
            link_info = zipfile.ZipInfo("xl/workbook.xml")
            link_info.create_system = 3
            link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(linked_member, "w") as archive:
                archive.writestr(link_info, "worksheets/sheet1.xml")
            with self.assertRaisesRegex(ValueError, "linked member"):
                company.profile_dataset(linked_member, "Guarded Data", allowed_root=approved)

            unsafe_xml = approved / "unsafe-xml.xlsx"
            with zipfile.ZipFile(source, "r") as original, zipfile.ZipFile(
                unsafe_xml, "w", compression=zipfile.ZIP_DEFLATED
            ) as rewritten:
                for info in original.infolist():
                    content = original.read(info)
                    if info.filename == "xl/workbook.xml":
                        content = content.replace(
                            b"<workbook",
                            b'<!DOCTYPE workbook [<!ENTITY x "unsafe">]><workbook',
                            1,
                        )
                    rewritten.writestr(info.filename, content)
            with self.assertRaisesRegex(ValueError, "declarations are unsafe"):
                company.profile_dataset(unsafe_xml, "Guarded Data", allowed_root=approved)

    @unittest.skipUnless(os.name == "nt", "Windows drive classification")
    def test_allow_root_rejects_a_dataset_root_on_a_non_local_drive(self):
        # _windows_drive_type()/_require_local_absolute() guard --allow-root
        # XLSX ingestion against non-local/network drives, but had zero test
        # coverage for either failure branch -- unlike the byte-for-byte
        # identical sibling implementation in scripts/check_readiness.py,
        # which tests/test_readiness.py deliberately mock-tests the same
        # way this test does. A future inverted check or swallowed
        # exception here would silently accept a REMOTE (type-4) drive as
        # local, weakening the exact safety property the rest of this file
        # tests for UNC/path-traversal rejection.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            approved = root / "approved"
            approved.mkdir()
            source = approved / "sales.xlsx"
            write_test_workbook(source)
            company = Company(root / "state", MockModel())
            company.create_project("Guarded Data")

            with patch(
                "local_company.spreadsheet._windows_drive_type", return_value=4,
            ) as drive_type:
                with self.assertRaisesRegex(ValueError, "must be on a local drive"):
                    company.profile_dataset(source, "Guarded Data", allowed_root=approved)
                drive_type.assert_called_once_with(approved.resolve().anchor)

            # A genuinely local (fixed) drive still works.
            with patch("local_company.spreadsheet._windows_drive_type", return_value=3):
                dataset_id, _brief_path, _profile = company.profile_dataset(
                    source, "Guarded Data", allowed_root=approved,
                )
            self.assertTrue(dataset_id)

    @unittest.skipUnless(os.name == "nt", "Windows drive classification")
    def test_windows_drive_type_fails_closed_when_the_probe_itself_errors(self) -> None:
        # _windows_drive_type()'s own try/except (wrapping the real
        # GetDriveTypeW ctypes call) had zero coverage -- mocking the
        # whole function (as the test above does, to control its return
        # value) bypasses this internal exception handling entirely, so
        # it needs a direct test of its own: a drive-check exception
        # (missing kernel32 attribute, OS error, ...) must fail closed as
        # SpreadsheetError, not propagate a raw ctypes exception or be
        # swallowed into treating the root as local.
        from local_company.spreadsheet import SpreadsheetError, _windows_drive_type

        with patch(
            "ctypes.windll.kernel32.GetDriveTypeW", side_effect=OSError("probe failed"),
        ):
            with self.assertRaisesRegex(SpreadsheetError, "could not be verified"):
                _windows_drive_type("C:\\")

    def test_sheet_option_is_rejected_for_non_xlsx_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "data.csv"
            source.write_text("name\nalpha\n", encoding="utf-8")
            company = Company(root / "state", MockModel())
            company.create_project("CSV Lab")
            with self.assertRaisesRegex(ValueError, "only for XLSX"):
                company.profile_dataset(source, "CSV Lab", sheet="Sales")


if __name__ == "__main__":
    unittest.main()
