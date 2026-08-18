from __future__ import annotations

import unittest
from unittest.mock import patch

from local_company.core import LOOPBACK_OLLAMA_HOST, default_ollama_host
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


class OllamaHostResolutionTests(unittest.TestCase):
    def test_unset_and_empty_resolve_to_loopback(self) -> None:
        for environment in ({}, {"LOCAL_COMPANY_OLLAMA_HOST": ""}):
            with self.subTest(environment=environment):
                with patch.dict("os.environ", environment, clear=True):
                    self.assertEqual(default_ollama_host(), LOOPBACK_OLLAMA_HOST)

    def test_container_sidecar_hosts_are_admitted(self) -> None:
        for value, expected in (
            ("http://ollama:11434", "http://ollama:11434"),
            ("http://ollama:11434/", "http://ollama:11434"),
            ("  http://ollama:11434  ", "http://ollama:11434"),
            ("https://inference.internal", "https://inference.internal"),
            ("http://[::1]:11434", "http://[::1]:11434"),
        ):
            with self.subTest(value=value):
                with patch.dict("os.environ", {"LOCAL_COMPANY_OLLAMA_HOST": value}, clear=True):
                    self.assertEqual(default_ollama_host(), expected)

    def test_malformed_endpoints_fail_closed_rather_than_using_loopback(self) -> None:
        for value in (
            "ollama:11434",
            "ftp://ollama:11434",
            "file:///etc/passwd",
            "http://ollama:11434/api/chat",
            "http://ollama:11434?x=1",
            "http://user:pass@ollama:11434",
            "http://ollama:99999",
            "http://ollama:notaport",
            "http://" + "h" * 300,
        ):
            with self.subTest(value=value):
                with patch.dict("os.environ", {"LOCAL_COMPANY_OLLAMA_HOST": value}, clear=True):
                    with self.assertRaises(ValueError):
                        default_ollama_host()


if __name__ == "__main__":
    unittest.main()
