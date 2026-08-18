# Contributing

Thank you for looking at this project. This file explains how to set it up, how
to run the tests, one workflow rule that is easy to miss, and the code style the
repository follows.

Please read [The one rule that cannot be bent](#the-one-rule-that-cannot-be-bent)
before you start writing code. It shapes what this project will and will not
accept.

## Set up

You need **Python 3.11 or newer**. That is all.

```bash
git clone <your-clone-url> local-agent-company
cd local-agent-company
python scripts/run_tests.py
```

There is **no `pip install` step**, because there is nothing to install. The
project has zero third-party dependencies and imports only the Python standard
library. `pyproject.toml` declares `dependencies = []`, and that is not an
accident to be fixed later — see the style rules below.

A `.venv` directory is present in the repository. It contains only `pip`. You do
**not** need to create it, activate it, or use it. The test runner adds the
repository root and `src/` to the import path itself, so you do not need an
editable install or a `PYTHONPATH` value either.

If you want the `local-company` console script on your `PATH`, `pip install -e .`
will give it to you, but every command also works as `python -m local_company.cli`
or through the `.cmd` launchers in the repository root.

Model-backed work additionally needs [Ollama](https://ollama.com) and a
supported model:

```bash
ollama pull llama3.2:1b
```

The test suite does **not** call a model, start a service, or create company
state, so you can develop and test with Ollama absent.

## Run the tests

```bash
# The full suite. This is the release check.
python scripts/run_tests.py

# One module, while you are working on it.
python scripts/run_tests.py --pattern test_model_policy.py

# Show individual test names when you need to diagnose a failure.
python scripts/run_tests.py --verbose
```

Notes:

- The runner treats **warnings as errors**. A new `DeprecationWarning` will fail
  the suite.
- It can be run from any working directory; it anchors itself to the repository.
- `--pattern` takes a `unittest` discovery filename pattern, default `test*.py`.
- A `--pattern` that matches nothing exits with code **2** and
  `no_tests_discovered`. Zero tests is never reported as a pass.
- Tests live in `tests/` and are named `test_*.py`. New test files are picked up
  automatically.

Run the full suite without a pattern before you open a pull request. A focused
pattern is for the middle of your work, not for the end of it.

## Re-stamp the build manifest after you change the source

**This is the rule people trip over.** If you skip it, tests fail and the error
message will not obviously point at you.

After editing anything under `src/local_company/`, or any of the fixed
operational scripts in `scripts/`, run:

```bash
python scripts/stamp_build_manifest.py --check
```

If it reports `Build manifest is stale`, re-stamp it:

```bash
python scripts/stamp_build_manifest.py --write --build-id local-build-YYYYMMDD.N
```

Use today's date and a revision number. For example, the first stamp on
18 August 2026 is `local-build-20260818.1`; the second stamp that same day is
`local-build-20260818.2`. The format is enforced: `local-build-` then eight
digits of date, a dot, then a revision from `1` to `9999` with no leading zero.
Build identity is monotonic, so you cannot stamp a build that sorts below the
current one.

### Why this gate exists

`src/local_company/build_info.py` is a generated file. It holds three values: a
schema name, a build ID, and `SOURCE_SHA256` — a digest over every Python file in
the package plus a fixed list of twelve lifecycle and orchestration scripts under
`scripts/`.

That digest is the project's **runtime build identity**. It exists so that:

- a running service can report exactly which build it is, without reading the
  filesystem or shelling out to Git while serving a health request;
- the readiness check, the runtime guard, and the runtime supervisor can compare
  the build **on disk** against the build that is **actually running**, and refuse
  to accept new work when those two disagree;
- an operator who copies a bundle to another machine can tell whether the code
  changed in transit.

If you edit the source and do not re-stamp, the embedded digest no longer
describes the code, and the tests that assert "the stamped identity matches the
operational source" fail. That failure is the drift detector doing its job. The
fix is always to re-stamp, never to relax the test.

One honest limitation, which is also stated in [SECURITY.md](SECURITY.md): the
manifest is a **drift detector, not a signature**. It proves the bytes have not
changed since they were stamped. It does not prove who stamped them.

## Code style

The house style is unusual in places. It is deliberate, and it is what makes the
safety claims in the README checkable.

### Standard library only, always

Never add a third-party import. Not a small one, not a dev-only one, not a
test-only one, not "just for this". Zero dependencies is a feature of the
product, not a temporary state. If a task seems to need a library, write the
bounded subset you actually need — that is why there is a stdlib XLSX reader and
a hand-written MCP server in this repository.

No code path may call `pip install` or download anything at import time.

### Check types exactly at trust boundaries

Where input crosses a boundary — a parsed JSON manifest, a subprocess result, a
value read from the database, an argument from the CLI — check the exact type:

```python
if type(required_runs) is not int or not 1 <= required_runs <= 10:
    return _blocked("required_runs_out_of_range")
```

`type(x) is not int` rather than `isinstance(x, int)` is intentional: `bool` is a
subclass of `int` in Python, and `isinstance(True, int)` is `True`. Exact checks
stop a `True` from being accepted where a count was expected. Inside a function,
after the boundary check has passed, ordinary `isinstance` is fine.

### Bound every read

Every read has an explicit maximum, and the maximum is a named constant. Bytes,
rows, columns, files, directory depth, recursion, list length, string length.
There is no "read the whole file" in this codebase. Look at the constants at the
top of `scripts/stamp_build_manifest.py` for the shape of this.

### Fail closed

When something is missing, malformed, ambiguous, unavailable, or changed while
you were looking at it, return a blocker. Do not guess, do not fall back to an
older value, do not proceed with a partial result and hope.

Distinguish the three outcomes clearly: **ready**, **a determinate action the
operator can take**, and **indeterminate**. Never report indeterminate as ready.
If two reads of the same source disagree, that is a failure, not a tie to break.

### Canonical JSON

Anything that gets hashed, sealed, compared, or written to a receipt is
serialised the same way every time:

```python
json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
```

Digests are only meaningful if the bytes are reproducible.

### Do not leak into contracts

Several JSON contracts are deliberately **pathless**: they must not contain
filesystem paths, objectives, source contents, digests of private data, model
output, or secrets. If you add a field to one of these, check the tests that
assert what the contract withholds — they exist on purpose.

### Keep Windows-only code import-safe everywhere

`computer_use.py` is Windows API code. It must still **import** on Linux and
macOS, and fail at call time with a clear reason, because other modules import it
at module scope. Guard the platform import, and gate the entry point on
`os.name == "nt"` when it is actually called. There is a regression test for this
that blocks `ctypes.wintypes` and asserts the modules still import.

## The one rule that cannot be bent

**No contribution may add an autonomous external action.**

This project never sends, spends, deploys, publishes, or deletes on its own.
There is deliberately no side-effect executor. Approving a request in the
approval inbox records a decision; it does not carry it out. A human performs the
real-world action.

Concretely, a pull request will be declined if it:

- sends email, messages, or webhooks;
- makes a payment, places an order, or moves funds;
- deploys, publishes, or pushes to a remote;
- uses stored credentials or an API key to act on a third-party service;
- deletes user data;
- adds an unattended loop that performs any of the above;
- or makes an outbound network request to anything other than the local model
  endpoint and the URLs a user explicitly asked the browser lane to read.

This is not a "not yet" list waiting for a good enough implementation. If an
external-action executor is ever built, it will be a separately designed,
separately audited component with its own threat model, not a feature slipped in
beside something else.

Related boundaries that follow from the same principle:

- Do not remove or weaken an owner gate to make a workflow smoother.
- Do not make the dashboard listen on anything other than loopback.
- Do not weaken a test assertion to make a build pass. Fix the code.
- Do not add a benchmark, a customer count, or a success claim to any document
  unless you can point at reproducible local evidence for it.

## Opening a pull request

1. One objective per pull request. Small and reviewable beats complete.
2. Add or update tests. A behaviour change with no test change is suspicious.
3. Run `python scripts/run_tests.py` — the full suite, no pattern.
4. Run `python scripts/stamp_build_manifest.py --check`, and re-stamp if stale.
5. Update the docs you affected. Operator behaviour lives in
   [docs/OPERATIONS.md](docs/OPERATIONS.md); product scope and honest limits live
   in [PRODUCT.md](PRODUCT.md).
6. In the description, say what you changed, what you tested, and what you could
   not verify. "I could not test this on Linux" is a useful sentence. A confident
   claim you did not check is not.

Please follow the [Code of Conduct](CODE_OF_CONDUCT.md). For security problems,
do not open a pull request or a public issue — see [SECURITY.md](SECURITY.md).

## Licensing of contributions

This project is licensed under the Apache License, Version 2.0. By submitting a
contribution you agree that it is licensed under the same terms, as described in
section 5 of the [LICENSE](LICENSE). There is no separate contributor licence
agreement to sign.
