"""Wraps scripts/check_fstring_python_version_compatibility.py as a normal test.

See that script's module docstring for why this exists: the local dev machine's
own interpreter cannot detect this class of bug by compiling, because it is new
enough that PEP 701 makes the old-illegal form valid. Only a dedicated scan
catches it here, and it only runs at all if it is wired into the suite that
CI and scripts/run_tests.py actually execute -- an unwired standalone script
would have caught nothing, the same way this exact bug went uncaught before.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from check_fstring_python_version_compatibility import find_incompatible_fstrings  # noqa: E402


class FstringPythonVersionCompatibilityTests(unittest.TestCase):
    def test_no_source_file_uses_a_pre_312_illegal_nested_fstring(self):
        root = Path(__file__).resolve().parents[1]
        findings: list[str] = []
        for subdir in ("src", "scripts", "tests"):
            for path in sorted((root / subdir).rglob("*.py")):
                for lineno, message in find_incompatible_fstrings(path.read_bytes()):
                    findings.append(f"{path.relative_to(root)}:{lineno}: {message}")
        self.assertEqual(
            findings, [],
            "pyproject.toml declares requires-python >=3.11; PEP 701 (3.12) made these "
            "forms legal, but on 3.11 each one is a SyntaxError that fails the WHOLE "
            "module's import, not just the one call site.",
        )

    def test_the_detector_catches_the_bug_it_exists_to_prevent(self) -> None:
        # A guard that has never been proven to catch anything is unverified. This is
        # the exact shape that shipped in dashboard.py before it was fixed: a
        # single-quoted f-string nested inside a double-quoted one, whose own
        # expression reuses that same single quote via a dict subscript.
        source = b"""item = {"queue_id": "q1"}\nx = f"{f' (queue {item['queue_id']})' if item['queue_id'] else ''}"\n"""
        hits = find_incompatible_fstrings(source)
        self.assertTrue(hits, "the detector must flag the known-bad pattern, or it is not testing anything")


if __name__ == "__main__":
    unittest.main()
