"""Replaceable, JSON-oriented LLM provider interface."""
from __future__ import annotations

import json
import logging
import math
import os
import time
from contextvars import ContextVar
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)
_REQUEST_DIAGNOSTIC: ContextVar[dict[str, Any] | None] = ContextVar("llm_request_diagnostic", default=None)
TRANSIENT_HTTP_STATUSES = {502, 503, 504}
MAX_TRANSIENT_RETRIES = 1
RETRY_BACKOFF_SECONDS = 0.2


class LLMError(RuntimeError):
    """Base error for provider failures."""


class LLMConfigurationError(LLMError):
    """Raised when no usable provider is configured."""


class LLMProviderError(LLMError):
    """Raised for provider transport or response failures."""

    def __init__(self, message: str, *, public_message: str = "LLM provider request failed") -> None:
        super().__init__(message)
        self.public_message = public_message


class LLMResponseFormatError(LLMProviderError):
    """Raised when a completed model response is malformed or schema-invalid."""

    def __init__(self, message: str = "LLM response did not match the required format") -> None:
        super().__init__(
            message,
            public_message="The language model returned an invalid structured response. Please rephrase the request and try again.",
        )


class LLMProvider(Protocol):
    def generate_json(
        self,
        *,
        system: str,
        prompt: str,
        response_model: type[BaseModel],
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> BaseModel: ...

    def generate_text(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> str: ...


class OpenAICompatibleProvider:
    """Chat-completions provider compatible with OpenAI and Ollama."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 30.0,
        provider_name: str = "openai-compatible",
    ) -> None:
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
        self.provider_name = provider_name

    def generate_json(
        self,
        *,
        system: str,
        prompt: str,
        response_model: type[BaseModel],
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> BaseModel:
        body = self._request_body(system, prompt, max_tokens, reasoning_effort)
        if self.provider_name == "ollama":
            # Ollama supports JSON Schema-constrained structured output. JSON
            # object mode alone guarantees syntax, not the Pydantic shape.
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"schema": response_model.model_json_schema()},
            }
        else:
            # Preserve the broad OpenAI-compatible behavior for providers that
            # do not share Ollama's JSON Schema response_format extension.
            body["response_format"] = {"type": "json_object"}
        payload = self._post_chat_completion(body)
        try:
            choice, content = self._extract_content(payload)
        except LLMProviderError:
            raise
        if choice.get("finish_reason") == "length":
            self._log_response_rejected("generation_limit_reached", validation_failed=False)
            raise LLMResponseFormatError("LLM response was truncated by its generation limit")
        if not content.strip():
            self._log_response_rejected("empty_message_content", validation_failed=False)
            raise LLMResponseFormatError("LLM provider returned empty structured response content")
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            self._log_response_rejected("invalid_json_content", exception_class=type(exc).__name__, validation_failed=True)
            raise LLMResponseFormatError("LLM provider returned content that was not valid JSON") from exc
        try:
            return response_model.model_validate(data)
        except ValidationError as exc:
            known_fields = set(response_model.model_fields)
            locations = [
                str(error["loc"][0]) if error["loc"] and error["loc"][0] in known_fields else "unexpected_field"
                for error in exc.errors(include_input=False)
            ]
            # Log Pydantic error classes only. Deeper locations can contain
            # model-generated dictionary keys that might echo private input.
            safe_issues = [error["type"] for error in exc.errors(include_input=False)]
            self._log_response_rejected(
                "response_schema_validation_failed", exception_class=type(exc).__name__,
                validation_failed=True, fields=",".join(locations) or "unknown",
                field_errors=",".join(safe_issues) or "unknown",
            )
            raise LLMResponseFormatError("LLM response did not match the required response schema") from exc

    def generate_text(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        """Generate ordinary assistant text without a structured JSON response mode."""
        body = self._request_body(system, prompt, max_tokens, reasoning_effort)
        payload = self._post_chat_completion(body)
        choice, content = self._extract_content(payload)
        if choice.get("finish_reason") == "length":
            self._log_response_rejected("generation_limit_reached", validation_failed=False)
            raise LLMProviderError("LLM response was truncated by its generation limit; try a shorter message")
        if not content.strip():
            self._log_response_rejected("empty_message_content", validation_failed=False)
            raise LLMProviderError("LLM provider returned empty response content")
        return content.strip()

    def _request_body(
        self,
        system: str,
        prompt: str,
        max_tokens: int | None,
        reasoning_effort: str | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if reasoning_effort is not None:
            body["reasoning_effort"] = reasoning_effort
        return body

    def _post_chat_completion(self, body: dict[str, Any]) -> Any:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        timeout = httpx.Timeout(self.timeout, connect=min(5.0, self.timeout), pool=min(5.0, self.timeout))
        started_at = time.monotonic()
        _REQUEST_DIAGNOSTIC.set({"duration_ms": 0, "http_status": None, "response_received": False})
        url = f"{self.base_url}/chat/completions"
        for attempt in range(MAX_TRANSIENT_RETRIES + 1):
            response_received = False
            logger.info(
                "LLM request started: provider=%s model=%s attempt=%s request_started=true generation_started=unconfirmed",
                self.provider_name, self.model, attempt + 1,
            )
            try:
                response = httpx.post(url, json=body, headers=headers, timeout=timeout)
                response_received = True
                _REQUEST_DIAGNOSTIC.set({
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                    "http_status": response.status_code,
                    "response_received": True,
                })
                if response.status_code in TRANSIENT_HTTP_STATUSES and attempt < MAX_TRANSIENT_RETRIES:
                    logger.warning(
                        "LLM transient response: category=transient_http_status provider=%s model=%s http_status=%s request_duration_ms=%s response_received=true retry=true",
                        self.provider_name, self.model, response.status_code,
                        int((time.monotonic() - started_at) * 1000),
                    )
                    time.sleep(RETRY_BACKOFF_SECONDS)
                    continue
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError as exc:
                    logger.warning(
                        "LLM response rejected: category=malformed_response_json provider=%s model=%s exception_class=%s request_duration_ms=%s response_received=true",
                        self.provider_name, self.model, type(exc).__name__, int((time.monotonic() - started_at) * 1000),
                    )
                    raise LLMProviderError("LLM provider returned malformed response data") from exc
                logger.info(
                    "LLM response received: provider=%s model=%s request_duration_ms=%s http_status=%s response_received=true generation_started=unconfirmed",
                    self.provider_name, self.model, int((time.monotonic() - started_at) * 1000), response.status_code,
                )
                _REQUEST_DIAGNOSTIC.set({
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                    "http_status": response.status_code,
                    "response_received": True,
                })
                return payload
            except httpx.TimeoutException as exc:
                logger.warning(
                    "LLM request failed: category=request_timeout provider=%s model=%s exception_class=%s request_duration_ms=%s request_started=true response_received=%s retry=false",
                    self.provider_name, self.model, type(exc).__name__, int((time.monotonic() - started_at) * 1000), str(response_received).lower(),
                )
                raise LLMProviderError("LLM request timed out", public_message="The local model took too long to respond. Please retry.") from exc
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                logger.warning(
                    "LLM request failed: category=http_status provider=%s model=%s exception_class=%s http_status=%s request_duration_ms=%s response_received=true retry=false",
                    self.provider_name, self.model, type(exc).__name__, status, int((time.monotonic() - started_at) * 1000),
                )
                message = "The local model service is temporarily unavailable. Please retry." if status in TRANSIENT_HTTP_STATUSES else f"The language model service returned HTTP {status}."
                raise LLMProviderError(
                    f"LLM provider returned HTTP {status}", public_message=message
                ) from exc
            except httpx.HTTPError as exc:
                transient_transport = isinstance(exc, (httpx.ConnectError, httpx.NetworkError))
                if transient_transport and attempt < MAX_TRANSIENT_RETRIES:
                    logger.warning(
                        "LLM transient transport failure: category=transient_transport provider=%s model=%s exception_class=%s request_duration_ms=%s response_received=false retry=true",
                        self.provider_name, self.model, type(exc).__name__, int((time.monotonic() - started_at) * 1000),
                    )
                    time.sleep(RETRY_BACKOFF_SECONDS)
                    continue
                logger.warning(
                    "LLM request failed: category=transport_error provider=%s model=%s exception_class=%s request_duration_ms=%s request_started=true response_received=%s retry=false",
                    self.provider_name, self.model, type(exc).__name__, int((time.monotonic() - started_at) * 1000), str(response_received).lower(),
                )
                raise LLMProviderError("LLM provider connection failed", public_message="Cannot connect to the language model service. Check that Ollama is running.") from exc
        raise LLMProviderError("LLM provider request failed")

    def _log_response_rejected(
        self,
        category: str,
        *,
        exception_class: str = "none",
        validation_failed: bool,
        fields: str = "none",
        field_errors: str = "none",
    ) -> None:
        diagnostic = _REQUEST_DIAGNOSTIC.get() or {}
        logger.warning(
            "LLM response rejected: category=%s provider=%s model=%s request_duration_ms=%s http_status=%s generation_started=unconfirmed response_received=%s validation_failed=%s exception_class=%s fields=%s field_errors=%s",
            category, self.provider_name, self.model, diagnostic.get("duration_ms", "unknown"),
            diagnostic.get("http_status") or "unknown", str(diagnostic.get("response_received", False)).lower(),
            str(validation_failed).lower(), exception_class, fields, field_errors,
        )

    def _extract_content(self, payload: Any) -> tuple[dict[str, Any], str]:
        try:
            choice = payload["choices"][0]
            message = choice["message"]
            content = message.get("content")
            if not isinstance(content, str):
                raise TypeError
            return choice, content
        except (AttributeError, KeyError, IndexError, TypeError) as exc:
            self._log_response_rejected(
                "unexpected_response_structure", exception_class=type(exc).__name__, validation_failed=True,
            )
            raise LLMProviderError("LLM provider returned an unexpected response structure") from exc


def get_provider_from_env() -> LLMProvider:
    """Select a configured provider; never silently fabricate an answer."""
    name = os.getenv("LLM_PROVIDER", "").strip().lower()
    if not name:
        raise LLMConfigurationError("No LLM provider configured; set LLM_PROVIDER and LLM_MODEL")
    model = os.getenv("LLM_MODEL", "").strip()
    default_timeout = "90" if name == "ollama" else "30"
    raw_timeout = os.getenv("LLM_TIMEOUT_SECONDS", default_timeout)
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
        return OpenAICompatibleProvider(base_url=base_url, model=model, api_key=key, timeout=timeout, provider_name="cloud")
    if name == "ollama":
        return OpenAICompatibleProvider(
            base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
            model=model,
            timeout=timeout,
            provider_name="ollama",
        )
    raise LLMConfigurationError(f"Unsupported LLM_PROVIDER '{name}'")
