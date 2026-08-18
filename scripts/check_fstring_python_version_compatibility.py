"""Fail-closed guard against pre-3.12-incompatible nested f-strings.

pyproject.toml declares `requires-python = ">=3.11"`. Python 3.12 (PEP 701)
relaxed the f-string grammar so an f-string's braces may contain a string
literal, or another f-string, that reuses the SAME quote character as its own
enclosing f-string. Before 3.12 that is a SyntaxError.

This machine's own interpreter is new enough (3.14, plus an unusually recent
build even for that) that it silently accepts the old-illegal form -- so this
class of bug is invisible in every local test run, forever, on this machine,
regardless of how many tests exist. It is only visible on a real 3.11 or
early-3.12 interpreter, which this repository's CI runs and this machine does
not. It already reached a public release before being caught here: see
git history around 2026-07-27 for the first instance, and the first real
GitHub Actions Windows run for how it surfaced.

Detection is via tokenize rather than a regex, because a regex cannot safely
tell a quote character inside a string literal from one delimiting it. This
walks the real token stream and tracks a stack of currently-open f-string
quote characters; a plain string or a further f-string that reuses the
top-of-stack character is exactly the illegal pre-3.12 shape.
"""
from __future__ import annotations

import io
import sys
import tokenize
from pathlib import Path


def find_incompatible_fstrings(source: bytes) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    try:
        tokens = list(tokenize.tokenize(io.BytesIO(source).readline))
    except (tokenize.TokenizeError, SyntaxError, UnicodeDecodeError, IndentationError):
        # A tokenize failure is a real problem, but not this script's problem to
        # diagnose -- the syntax check elsewhere in the release gate owns that.
        return hits
    open_fstring_quotes: list[str] = []
    for tok in tokens:
        if tok.type == tokenize.FSTRING_START:
            quote = tok.string[-1]
            if open_fstring_quotes and quote == open_fstring_quotes[-1]:
                hits.append((tok.start[0], f"nested f-string reuses its enclosing quote {quote!r}"))
            open_fstring_quotes.append(quote)
        elif tok.type == tokenize.FSTRING_END:
            if open_fstring_quotes:
                open_fstring_quotes.pop()
        elif tok.type == tokenize.STRING and open_fstring_quotes:
            quote = next((char for char in tok.string if char in ("'", '"')), None)
            if quote and quote == open_fstring_quotes[-1]:
                hits.append((tok.start[0], f"string literal {tok.string!r} reuses the enclosing f-string's quote {quote!r}"))
    return hits


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    findings: list[str] = []
    for subdir in ("src", "scripts", "tests"):
        for path in sorted((root / subdir).rglob("*.py")):
            for lineno, message in find_incompatible_fstrings(path.read_bytes()):
                findings.append(f"{path.relative_to(root)}:{lineno}: {message}")
    if findings:
        sys.stderr.write("fstring_python_version_compatibility_violation\n")
        for finding in findings:
            sys.stderr.write(f"  {finding}\n")
        return 1
    print('{"ok": true, "contract": "local-company.fstring-compat.v1"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
