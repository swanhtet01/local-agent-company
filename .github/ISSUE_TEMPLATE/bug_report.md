---
name: Bug report
about: Something behaves incorrectly, crashes, or reports a wrong result
title: "bug: "
labels: ["bug"]
assignees: []
---

## What happened

<!-- One or two sentences. Include the exact error or the wrong output. -->

## What you expected instead

## How to reproduce

<!-- Exact commands. Minimal is better than complete. -->

```
1.
2.
3.
```

## Environment

| | |
| --- | --- |
| OS and version | <!-- e.g. Windows 11 24H2 / Ubuntu 24.04 in Docker / Fedora 41 --> |
| Python version | <!-- output of `python -VV` --> |
| Install method | <!-- git clone / pilot bundle / container --> |
| Commit or build ID | <!-- `git rev-parse --short HEAD`, and BUILD_ID from src/local_company/build_info.py --> |

This project was Windows-only until recently and the Linux port is still being
proven, so the OS line genuinely decides how the report is triaged.

## Local model runtime

| | |
| --- | --- |
| Is Ollama running? | <!-- yes / no --> |
| Which model | <!-- e.g. llama3.2:1b (the default) or llama3.2:3b --> |
| Ollama host | <!-- default is http://127.0.0.1:11434 --> |
| Other runtime in use | <!-- LM Studio / OpenCode / none --> |

Answer these even if the bug looks unrelated to inference - several code paths
gate on the model runtime being reachable and fail closed when it is not.

## On Linux: port verification receipt

Run this and paste the whole JSON object, even when it passes:

```
python deploy/verify_linux_port.py
```

Add `--offline` if Ollama is not running (it still reports the configured
host). Inside the container the command is:

```
docker compose exec coordinator python /app/deploy/verify_linux_port.py
```

```json

```

## Evidence

Receipts beat descriptions. Paste whichever you have:

```
python scripts/run_tests.py
python scripts/run_tests.py --pattern test_<area>.py --verbose
python scripts/stamp_build_manifest.py --check
```

```

```

## Checklist

- [ ] I removed secrets, tokens, absolute local paths, machine names, and
      customer data from everything pasted above.
- [ ] I searched existing issues first.
