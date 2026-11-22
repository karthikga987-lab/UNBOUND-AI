"""Replaceable, JSON-oriented LLM provider interface."""
from __future__ import annotations

import json
import math
import os
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ValidationError


class LLMError(RuntimeError):
    """Base error for provider failures."""


class LLMConfigurationError(LLMError):
    """Raised when no usable provider is configured."""


class LLMProviderError(LLMError):
    """Raised for provider transport or response failures."""


class LLMProvider(Protocol):
    def generate_json(self, *, system: str, prompt: str, response_model: type[BaseModel]) -> BaseModel: ...


class OpenAICompatibleProvider:
    """Chat-completions provider compatible with OpenAI and Ollama."""

    def __init__(self, *, base_url: str, model: str, api_key: str | None = None, timeout: float = 30.0) -> None:
        if not model:
            raise LLMConfigurationError("LLM_MODEL must be configured")
        try:
            timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise LLMConfigurationError("LLM timeout must be a positive finite number of seconds") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise LLMConfigurationError("LLM timeout must be a positive finite number of seconds")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def generate_json(self, *, system: str, prompt: str, response_model: type[BaseModel]) -> BaseModel:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        try:
            response = httpx.post(f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            data = json.loads(content)
            return response_model.model_validate(data)
        except httpx.TimeoutException as exc:
            raise LLMProviderError("LLM request timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise LLMProviderError(f"LLM provider returned HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, ValidationError) as exc:
            raise LLMProviderError(f"LLM provider returned an invalid response: {exc}") from exc


def get_provider_from_env() -> LLMProvider:
    """Select a configured provider; never silently fabricate an answer."""
    name = os.getenv("LLM_PROVIDER", "").strip().lower()
    if not name:
        raise LLMConfigurationError("No LLM provider configured; set LLM_PROVIDER and LLM_MODEL")
    model = os.getenv("LLM_MODEL", "").strip()
    raw_timeout = os.getenv("LLM_TIMEOUT_SECONDS", "30")
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise LLMConfigurationError("LLM_TIMEOUT_SECONDS must be a positive finite number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise LLMConfigurationError("LLM_TIMEOUT_SECONDS must be a positive finite number")
    if name in {"openai", "openai-compatible", "cloud"}:
        base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
        key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
        if not key:
            raise LLMConfigurationError("Cloud provider selected but OPENAI_API_KEY (or LLM_API_KEY) is not set")
        return OpenAICompatibleProvider(base_url=base_url, model=model, api_key=key, timeout=timeout)
    if name == "ollama":
        return OpenAICompatibleProvider(base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"), model=model, timeout=timeout)
    raise LLMConfigurationError(f"Unsupported LLM_PROVIDER '{name}'")
