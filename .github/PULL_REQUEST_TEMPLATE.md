# What changed

<!-- One paragraph. What does this PR do, and why? -->

## Evidence

This project runs on receipts, not assertions. Paste the real output.

<details>
<summary>Test suite receipt</summary>

```json
<!-- python scripts/run_tests.py -->
```

</details>

<details>
<summary>Build manifest receipt (only if you touched operational source)</summary>

```json
<!-- python scripts/stamp_build_manifest.py --check -->
```

</details>

<!-- Also paste any focused run you relied on, e.g.
     python scripts/run_tests.py --pattern test_computer_use.py --verbose -->

## Checklist

- [ ] Tests pass locally via `python scripts/run_tests.py` (exit code `0`, receipt `"status":"passed"`), and the receipt is pasted above.
- [ ] I re-stamped the build manifest if I edited `src/local_company/` - `python scripts/stamp_build_manifest.py --write --build-id local-build-YYYYMMDD.N` - and committed the regenerated `src/local_company/build_info.py`.
- [ ] I added or changed tests to cover this change, and I can name the test that would have caught the bug.
- [ ] No new third-party dependency. `pyproject.toml` keeps `dependencies = []`; this project is stdlib-only on purpose.
- [ ] Still Python 3.11+ compatible (no syntax or stdlib API newer than 3.11).
- [ ] Cross-platform: I said below what I actually ran on. This project was Windows-only until recently and the Linux port is still being proven.
- [ ] No secrets, tokens, absolute local paths, machine names, or customer data in the diff or in the pasted evidence.

## Owner gates

This project never acts on the world without its owner. Confirm the change
keeps it that way:

- [ ] This change does not send, deploy, spend, publish, push, merge, message
      anyone, use credentials, enable hosted writes, or expose a local server
      beyond `127.0.0.1` - autonomously or as a side effect.
- [ ] Any new capability that *could* reach outside the machine stays behind an
      explicit owner confirmation, is off by default, and fails closed.
- [ ] Any new observation that cannot be made is reported as blocked, not
      silently treated as a pass.

## Platforms I ran this on

<!-- Delete what does not apply, and say which Python. -->

- Windows: <!-- e.g. Windows 11, Python 3.13 -->
- Linux: <!-- e.g. Ubuntu 24.04 container, Python 3.12, plus the
     `python deploy/verify_linux_port.py` receipt - or "not run" -->

<!-- CI runs both, six legs total (ubuntu-latest and windows-latest, Python
     3.11-3.13), and the `required` job gates the merge on all six. -->
