from __future__ import annotations


DEFAULT_LOCAL_MODEL = "llama3.2:1b"
SUPPORTED_LOCAL_MODELS = frozenset({DEFAULT_LOCAL_MODEL, "llama3.2:3b"})
PREFERRED_LOCAL_MODELS = ("llama3.2:3b", DEFAULT_LOCAL_MODEL)


def is_supported_local_model(model: object) -> bool:
    return type(model) is str and model.strip().lower() in SUPPORTED_LOCAL_MODELS


def require_local_llama_model(model: object) -> str:
    if type(model) is not str:
        raise ValueError("local_model_must_be_llama")
    normalized = model.strip().lower()
    if normalized not in SUPPORTED_LOCAL_MODELS:
        raise ValueError("local_model_must_be_llama")
    return normalized
