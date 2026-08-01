# Local Agent Company operating guide

Use this repository to run local-first business teams and coding agents without a paid inference API by default.

## Runtime selection

- Use Ollama plus OpenCode for terminal coding work through `local-code.cmd`.
- Use Bionic Code Projects as an alternative local coding agent and Work Projects for document, research, and analysis tasks.
- Use LM Studio for model evaluation or an optional OpenAI-compatible loopback API.
- Run one inference runtime at a time on the ROG Ally. Let `local-code.cmd` prefer `qwen3.5:4b` only when its current-memory gate passes; otherwise use `qwen3.5:0.8b` for small bounded work. The 0.8B model requires at least 2 GiB currently available so its measured cold-start footprint does not consume the OS reserve.
- Keep servers on `127.0.0.1`, models scale-to-zero, and paid/cloud models opt-in only.

## Execution

1. Lock one objective, exact deliverable, and at most five in-scope paths.
2. Inspect current Git state and preserve concurrent changes.
3. Use `local-code.cmd --check PROJECT_PATH` before local coding inference.
4. For unattended experiments, use `local-code.cmd --run PROJECT TASK_FILE --protect TEST_FILE --test COMMAND ...`. Treat only a `local-ai.coding-run.v1` receipt with `status=accepted`, changed files, current protected files, and passing tests as success.
5. Run focused checks during work and `python scripts/run_tests.py` once for a completed repository slice.
6. Review diffs and evidence before keeping model-generated changes.

Never autonomously push, merge, deploy, publish, message customers, spend money, use credentials, enable hosted writes, or expose a local model server to the network.
