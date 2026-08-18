"""Render the CI receipts into the GitHub Actions job summary.

Both CI gates emit a single JSON object:

  * ``scripts/stamp_build_manifest.py --check`` prints its receipt on stdout
    when the manifest is current and on stderr when it is stale.
  * ``scripts/run_tests.py`` prints a ``local-company.tests.v3`` receipt on
    stdout; the unittest report goes to stderr.

This script turns whichever receipts exist into a short markdown block on
``$GITHUB_STEP_SUMMARY`` so a failing leg is readable from the PR page
without opening the raw log. It is stdlib-only and never fails the job: a
missing or unparsable receipt is reported as such, not raised.

Run it locally with ``GITHUB_STEP_SUMMARY`` unset to print to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


MAX_RECEIPT_BYTES = 256 * 1024
MAX_RAW_LINES = 20


def _load_receipt(path: Path) -> tuple[dict[str, object] | None, str]:
    """Return (parsed receipt, raw text). Either half may be empty."""
    try:
        if not path.is_file():
            return None, ""
        raw = path.read_text(encoding="utf-8", errors="replace")[:MAX_RECEIPT_BYTES]
    except OSError as exc:
        return None, f"could not read {path.name}: {exc}"
    for line in reversed(raw.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed, raw
    return None, raw


def _icon(ok: bool | None) -> str:
    if ok is None:
        return "?"
    return "pass" if ok else "FAIL"


def _manifest_row(receipt: dict[str, object] | None) -> tuple[str, bool | None, str]:
    if receipt is None:
        return "Build manifest", None, "no receipt (step did not run)"
    status = str(receipt.get("status", "unknown"))
    if status == "ok":
        digest = str(receipt.get("source_sha256", ""))
        detail = "build `{build}` - {count} files - sha256 `{digest}`".format(
            build=receipt.get("build_id", "?"),
            count=receipt.get("file_count", "?"),
            digest=digest[:12] + "..." if len(digest) > 12 else digest or "?",
        )
        return "Build manifest", True, detail
    return "Build manifest", False, str(receipt.get("error", status))


def _tests_row(receipt: dict[str, object] | None) -> tuple[str, bool | None, str]:
    if receipt is None:
        return "Test suite", None, "no receipt (step did not run)"
    status = str(receipt.get("status", "unknown"))
    detail = (
        "{run} run - {failures} failures - {errors} errors - {skipped} skipped"
    ).format(
        run=receipt.get("tests_run", "?"),
        failures=receipt.get("failures", "?"),
        errors=receipt.get("errors", "?"),
        skipped=receipt.get("skipped", "?"),
    )
    reason = str(receipt.get("reason", "none"))
    if reason not in ("none", ""):
        detail = f"{detail} - reason: `{reason}`"
    return "Test suite", status == "passed", detail


def _raw_block(title: str, raw: str) -> list[str]:
    lines = [line for line in raw.splitlines() if line.strip()][-MAX_RAW_LINES:]
    if not lines:
        return []
    return [
        "<details><summary>{title} receipt</summary>".format(title=title),
        "",
        "```json",
        *lines,
        "```",
        "",
        "</details>",
        "",
    ]


def build_summary(
    *, label: str, manifest_path: Path, test_path: Path,
) -> str:
    manifest_receipt, manifest_raw = _load_receipt(manifest_path)
    test_receipt, test_raw = _load_receipt(test_path)

    rows = [_manifest_row(manifest_receipt), _tests_row(test_receipt)]
    failed = [name for name, ok, _ in rows if ok is False]

    out: list[str] = [f"## {label}", ""]
    out.append("| gate | result | detail |")
    out.append("| --- | --- | --- |")
    for name, ok, detail in rows:
        out.append(f"| {name} | {_icon(ok)} | {detail} |")
    out.append("")

    if "Build manifest" in failed:
        out.extend(
            [
                "### How to fix the build manifest",
                "",
                "The embedded SHA-256 in `src/local_company/build_info.py` no longer",
                "covers the operational source. Re-stamp it locally and commit the",
                "regenerated manifest:",
                "",
                "```",
                "python scripts/stamp_build_manifest.py --write --build-id local-build-YYYYMMDD.N",
                "```",
                "",
                "Coverage is every `.py` under `src/local_company/` except",
                "`build_info.py`, plus the 12 allowlisted lifecycle files under",
                "`scripts/`. The build ID must not move backward.",
                "",
            ]
        )

    if "Test suite" in failed:
        out.extend(
            [
                "### Failing tests",
                "",
                "The JSON receipt above counts the failures; the unittest report",
                "with tracebacks is on stderr in the **Run the test suite** step of",
                "this job's log.",
                "",
                "Reproduce locally:",
                "",
                "```",
                "python scripts/run_tests.py",
                "python scripts/run_tests.py --pattern test_computer_use.py --verbose",
                "```",
                "",
            ]
        )

    # Always surface the raw receipts - the JSON is the project's evidence
    # artefact, and a rendered table is a convenience on top of it, never a
    # replacement for it.
    out.extend(_raw_block("Build manifest", manifest_raw))
    out.extend(_raw_block("Test suite", test_raw))

    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="CI leg", help="heading for this matrix leg")
    parser.add_argument(
        "--manifest-receipt",
        default="manifest-receipt.json",
        help="path to the stamp_build_manifest.py --check receipt",
    )
    parser.add_argument(
        "--test-receipt",
        default="test-receipt.json",
        help="path to the run_tests.py receipt",
    )
    args = parser.parse_args(argv)

    summary = build_summary(
        label=args.label,
        manifest_path=Path(args.manifest_receipt),
        test_path=Path(args.test_receipt),
    )

    destination = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if destination:
        try:
            with open(destination, "a", encoding="utf-8") as handle:
                handle.write(summary)
        except OSError as exc:
            # Never fail a job over its own summary.
            print(f"could not write job summary: {exc}", file=sys.stderr)
            print(summary)
    else:
        print(summary, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
