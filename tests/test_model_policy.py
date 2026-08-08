from __future__ import annotations

import unittest

from local_company.model_policy import (
    DEFAULT_LOCAL_MODEL,
    SUPPORTED_LOCAL_MODELS,
    is_supported_local_model,
    require_local_llama_model,
)


class LocalModelPolicyTests(unittest.TestCase):
    def test_default_is_low_memory_llama(self) -> None:
        self.assertEqual(DEFAULT_LOCAL_MODEL, "llama3.2:1b")
        self.assertEqual(
            SUPPORTED_LOCAL_MODELS,
            frozenset({"llama3.2:1b", "llama3.2:3b"}),
        )

    def test_supported_llama_name_is_normalized(self) -> None:
        self.assertTrue(is_supported_local_model(" LLAMA3.2:1B "))
        self.assertEqual(require_local_llama_model(" LLAMA3.2:1B "), "llama3.2:1b")

    def test_other_model_families_fail_closed(self) -> None:
        for value in ("qwen3.5:0.8b", "mistral:7b", "deepseek-r1:1.5b", "", None):
            with self.subTest(value=value):
                self.assertFalse(is_supported_local_model(value))
                with self.assertRaisesRegex(ValueError, "local_model_must_be_llama"):
                    require_local_llama_model(value)


if __name__ == "__main__":
    unittest.main()
