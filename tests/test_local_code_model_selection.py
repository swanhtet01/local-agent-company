from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from scripts.select_local_code_model import GIB, main, select_model


class LocalCodeModelSelectionTests(unittest.TestCase):
    def test_prefers_quality_model_only_when_installed_and_memory_admitted(self) -> None:
        selected = select_model({"qwen3.5:4b", "qwen3.5:0.8b"}, 6 * GIB)
        self.assertEqual(selected.model, "qwen3.5:4b")
        self.assertEqual(selected.reason, "quality_model_admitted")
        constrained = select_model({"qwen3.5:4b", "qwen3.5:0.8b"}, 2 * GIB)
        self.assertEqual(constrained.model, "qwen3.5:0.8b")
        self.assertEqual(constrained.reason, "bootstrap_model_admitted")

    def test_explicit_model_request_still_obeys_installation_and_memory(self) -> None:
        selected = select_model({"qwen3.5:4b", "qwen3.5:0.8b"}, 6 * GIB, "qwen3.5:4b")
        self.assertEqual(selected.reason, "explicit_request_admitted")
        with self.assertRaisesRegex(ValueError, "requested_model_memory_blocked"):
            select_model({"qwen3.5:4b"}, 4 * GIB, "qwen3.5:4b")
        with self.assertRaisesRegex(ValueError, "requested_model_not_installed"):
            select_model({"qwen3.5:0.8b"}, 6 * GIB, "qwen3.5:4b")
        with self.assertRaisesRegex(ValueError, "requested_model_unsupported"):
            select_model({"other:latest"}, 20 * GIB, "other:latest")

    def test_fails_when_no_installed_model_fits_instead_of_overcommitting(self) -> None:
        with self.assertRaisesRegex(ValueError, "installed_models_memory_blocked"):
            select_model({"qwen3.5:4b", "qwen3.5:0.8b"}, GIB - 1)
        with self.assertRaisesRegex(ValueError, "supported_model_not_installed"):
            select_model(set(), 20 * GIB)

    def test_cli_receipt_is_read_only_and_model_only_output_is_exact(self) -> None:
        with patch("scripts.select_local_code_model.installed_ollama_models", return_value={"qwen3.5:0.8b"}), patch(
            "scripts.select_local_code_model.available_memory_bytes", return_value=2 * GIB,
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main([]), 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["model"], "qwen3.5:0.8b")
            self.assertFalse(receipt["controls"]["modelLoaded"])
            self.assertFalse(receipt["controls"]["externalRequestPerformed"])
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--model-only"]), 0)
            self.assertEqual(output.getvalue(), "qwen3.5:0.8b\n")

    def test_cli_blocker_is_bounded_and_nonzero(self) -> None:
        with patch("scripts.select_local_code_model.installed_ollama_models", return_value={"qwen3.5:4b"}), patch(
            "scripts.select_local_code_model.available_memory_bytes", return_value=2 * GIB,
        ):
            error = io.StringIO()
            with redirect_stderr(error):
                self.assertEqual(main(["--requested-model", "qwen3.5:4b"]), 1)
            self.assertEqual(json.loads(error.getvalue())["reason"], "requested_model_memory_blocked")


if __name__ == "__main__":
    unittest.main()
