from __future__ import annotations

import argparse
import json
import os
import sys
import unittest
import warnings
from pathlib import Path


SCHEMA = "local-company.tests.v3"


def project_root() -> Path:
    return Path(__file__).resolve(strict=True).parents[1]


def configure_import_paths(root: Path) -> None:
    for path in (root / "src", root):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)


def build_suite(root: Path, pattern: str) -> unittest.TestSuite:
    tests = root / "tests"
    return unittest.defaultTestLoader.discover(
        start_dir=str(tests),
        pattern=pattern,
        top_level_dir=str(tests),
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Run the dependency-free Local Agent Company test suite.",
    )
    result.add_argument(
        "--pattern",
        default="test*.py",
        help="unittest discovery filename pattern (default: test*.py)",
    )
    result.add_argument(
        "--verbose",
        action="store_true",
        help="print every test name instead of the concise release summary",
    )
    return result


def emit_summary(
    *,
    errors: int,
    failures: int,
    reason: str,
    skipped: int,
    status: str,
    tests_run: int,
    verbose: bool,
) -> None:
    print(
        json.dumps(
            {
                "errors": errors,
                "failures": failures,
                "reason": reason,
                "schema": SCHEMA,
                "skipped": skipped,
                "status": status,
                "tests_run": tests_run,
                "verbosity": "verbose" if verbose else "concise",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = project_root()
    configure_import_paths(root)
    warnings.simplefilter("error")
    os.environ["PYTHONWARNINGS"] = "error"

    previous_directory = Path.cwd()
    try:
        os.chdir(root)
        suite = build_suite(root, args.pattern)
        if suite.countTestCases() == 0:
            emit_summary(
                errors=0,
                failures=0,
                reason="no_tests_discovered",
                skipped=0,
                status="invalid",
                tests_run=0,
                verbose=args.verbose,
            )
            return 2
        result = unittest.TextTestRunner(verbosity=2 if args.verbose else 0).run(
            suite
        )
    finally:
        os.chdir(previous_directory)

    successful = result.wasSuccessful()
    emit_summary(
        errors=len(result.errors),
        failures=len(result.failures),
        reason="none" if successful else "test_failures",
        skipped=len(result.skipped),
        status="passed" if successful else "failed",
        tests_run=result.testsRun,
        verbose=args.verbose,
    )
    return 0 if successful else 1


if __name__ == "__main__":
    raise SystemExit(main())
