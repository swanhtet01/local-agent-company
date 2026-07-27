from __future__ import annotations

import argparse
import json
import os
import sys
import unittest
import warnings
from pathlib import Path


SCHEMA = "local-company.tests.v1"


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
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = project_root()
    configure_import_paths(root)
    warnings.simplefilter("error")
    os.environ["PYTHONWARNINGS"] = "error"

    previous_directory = Path.cwd()
    try:
        os.chdir(root)
        result = unittest.TextTestRunner(verbosity=2).run(
            build_suite(root, args.pattern)
        )
    finally:
        os.chdir(previous_directory)

    status = "passed" if result.wasSuccessful() else "failed"
    print(
        json.dumps(
            {
                "errors": len(result.errors),
                "failures": len(result.failures),
                "schema": SCHEMA,
                "skipped": len(result.skipped),
                "status": status,
                "tests_run": result.testsRun,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
