"""Replaceable, JSON-oriented LLM provider interface."""
from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import re
import time
from contextvars import ContextVar
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)
_REQUEST_DIAGNOSTIC: ContextVar[dict[str, Any] | None] = ContextVar("llm_request_diagnostic", default=None)
TRANSIENT_HTTP_STATUSES = {502, 503, 504}
MAX_TRANSIENT_RETRIES = 1
RETRY_BACKOFF_SECONDS = 0.2
_SAFE_DIAGNOSTIC_TOKEN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _safe_http_error_diagnostics(response: httpx.Response) -> tuple[str, str, str]:
    """Extract only bounded error metadata; provider bodies may echo prompts."""
    category = "unclassified_provider_error"
    code = "unknown"
    parameter = "none"
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        raw_code = error.get("code") or error.get("type")
        raw_parameter = error.get("param") or error.get("parameter")
        if isinstance(raw_code, str) and _SAFE_DIAGNOSTIC_TOKEN.fullmatch(raw_code):
            code = raw_code
        if isinstance(raw_parameter, str) and _SAFE_DIAGNOSTIC_TOKEN.fullmatch(raw_parameter):
            parameter = raw_parameter
        if parameter != "none" or code in {"invalid_request_error", "unsupported_value", "unsupported_parameter"}:
            category = "request_parameter_rejected"
        else:
            category = "structured_provider_error"
    elif isinstance(error, str):
        # Classify common OpenAI-compatible validation responses without ever
        # logging or returning the raw message, which may contain user input.
        unknown = re.search(r"(?i)(?:unknown|unsupported|unrecognized)\s+(?:field|parameter)\s+['\"`]?([a-zA-Z0-9_.-]{1,64})", error)
        invalid = re.search(r"(?i)invalid\s+(?:field|parameter)\s+['\"`]?([a-zA-Z0-9_.-]{1,64})", error)
        match = unknown or invalid
        if match and _SAFE_DIAGNOSTIC_TOKEN.fullmatch(match.group(1)):
            category = "request_parameter_rejected"
            parameter = match.group(1)
            code = "unsupported_parameter" if unknown else "invalid_parameter"
        elif re.search(r"(?i)model.{0,24}(?:not found|does not exist)", error):
            category = "model_unavailable"
            code = "model_not_found"
        else:
            category = "provider_rejected_request"
    return category, code, parameter


def _request_shape(body: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return content-free request metadata suitable for operational logs."""
    messages = body.get("messages")
    roles = ",".join(str(item.get("role", "unknown")) for item in messages if isinstance(item, dict)) if isinstance(messages, list) else "unknown"
    response_format = body.get("response_format")
    format_type = response_format.get("type", "none") if isinstance(response_format, dict) else "none"
    reasoning = body.get("reasoning_effort", "omitted")
    return roles[:64], str(body.get("max_tokens", "omitted")), str(reasoning)[:24], str(format_type)[:32]


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
        request_roles, max_tokens, reasoning_effort, response_format = _request_shape(body)
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
                error_category, error_code, error_parameter = _safe_http_error_diagnostics(exc.response)
                logger.warning(
                    "LLM request failed: category=http_status provider=%s model=%s endpoint_path=/chat/completions exception_class=%s http_status=%s request_duration_ms=%s response_received=true retry=false provider_error_category=%s provider_error_code=%s provider_error_parameter=%s request_roles=%s max_tokens=%s reasoning_effort=%s response_format=%s",
                    self.provider_name, self.model, type(exc).__name__, status,
                    int((time.monotonic() - started_at) * 1000), error_category,
                    error_code, error_parameter, request_roles, max_tokens,
                    reasoning_effort, response_format,
                )
                if status in TRANSIENT_HTTP_STATUSES:
                    message = "The local model service is temporarily unavailable. Please retry."
                elif status == 400 and self.provider_name == "ollama" and error_parameter != "none":
                    message = f"Ollama rejected request parameter '{error_parameter}' (HTTP 400). Check compatibility with the configured local model service."
                elif status == 400 and self.provider_name == "ollama":
                    message = f"Ollama rejected the request (HTTP 400; {error_category}). The request was not retried with a different model."
                else:
                    message = f"The language model service returned HTTP {status}."
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


def _ollama_error_summary(response: httpx.Response) -> tuple[str, str]:
    """Return allowlisted provider diagnostics without echoing request content."""
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return "unclassified_provider_error", "The local model rejected the request."
    raw_error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(raw_error, str):
        return "unclassified_provider_error", "The local model rejected the request."

    lowered = raw_error.casefold()
    if "model" in lowered and ("not found" in lowered or "does not exist" in lowered):
        return "model_unavailable", "The configured Ollama model was not found."
    parameter = re.search(
        r"(?i)(?:unknown|unsupported|unrecognized|invalid)\s+(?:field|parameter)\s+['\"`]?([A-Za-z0-9_.-]{1,64})",
        raw_error,
    )
    if parameter and _SAFE_DIAGNOSTIC_TOKEN.fullmatch(parameter.group(1)):
        return "request_parameter_rejected", f"Ollama rejected request parameter '{parameter.group(1)}'."
    if "unsupported value" in lowered or "invalid value" in lowered:
        return "request_value_rejected", "Ollama rejected a request value."
    # Native error strings may echo the complete prompt or document content.
    # Never expose/log unclassified provider text; preserve only its digest and
    # byte count in the caller's structured diagnostic.
    return "provider_rejected_request", "The local model rejected the request."


class OllamaNativeProvider:
    """Ollama's native /api/chat adapter, including native thinking and schema controls."""

    provider_name = "ollama"

    def __init__(self, *, base_url: str, model: str, timeout: float = 90.0) -> None:
        if not isinstance(model, str) or not model.strip():
            raise LLMConfigurationError("LLM_MODEL must be configured")
        try:
            timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise LLMConfigurationError("LLM timeout must be a positive finite number of seconds") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise LLMConfigurationError("LLM timeout must be a positive finite number of seconds")
        try:
            parsed = urlsplit(base_url.rstrip("/"))
            port = parsed.port
        except (AttributeError, TypeError, ValueError):
            raise LLMConfigurationError("LLM_BASE_URL is not a valid Ollama endpoint") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/", "/v1"}
        ):
            raise LLMConfigurationError("LLM_BASE_URL must be an Ollama server URL, optionally ending in /v1")
        authority = parsed.hostname
        if ":" in authority and not authority.startswith("["):
            authority = f"[{authority}]"
        if port is not None:
            authority = f"{authority}:{port}"
        self.base_url = urlunsplit((parsed.scheme, authority, "", "", ""))
        self.model = model.strip()
        self.timeout = timeout

    @staticmethod
    def _messages(system: str, prompt: str) -> list[dict[str, str]]:
        return [{"role": "system", "content": system}, {"role": "user", "content": prompt}]

    def _request_body(
        self,
        system: str,
        prompt: str,
        max_tokens: int | None,
        response_model: type[BaseModel] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(system, prompt),
            "stream": False,
            # Qwen's internal reasoning otherwise consumes the small bounded
            # output budgets used by intent and plan generation. Keep it out
            # of both user-facing text and structured result content.
            "think": False,
        }
        if max_tokens is not None:
            if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 1 <= max_tokens <= 8192:
                raise ValueError("max_tokens must be an integer between 1 and 8192")
            body["options"] = {"num_predict": max_tokens}
        if response_model is not None:
            body["format"] = response_model.model_json_schema()
        return body

    def _post_chat(self, body: dict[str, Any]) -> dict[str, Any]:
        timeout = httpx.Timeout(self.timeout, connect=min(5.0, self.timeout), pool=min(5.0, self.timeout))
        started_at = time.monotonic()
        _REQUEST_DIAGNOSTIC.set({"duration_ms": 0, "http_status": None, "response_received": False})
        url = f"{self.base_url}/api/chat"
        roles, max_tokens, _reasoning, response_format = _request_shape({
            "messages": body.get("messages"),
            "max_tokens": body.get("options", {}).get("num_predict", "omitted"),
            "response_format": {"type": "json_schema"} if "format" in body else None,
        })
        for attempt in range(MAX_TRANSIENT_RETRIES + 1):
            try:
                response = httpx.post(url, json=body, headers={"Content-Type": "application/json"}, timeout=timeout)
                _REQUEST_DIAGNOSTIC.set({
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                    "http_status": response.status_code,
                    "response_received": True,
                })
                if response.status_code in TRANSIENT_HTTP_STATUSES and attempt < MAX_TRANSIENT_RETRIES:
                    logger.warning(
                        "LLM transient response: category=transient_http_status provider=ollama model=%s endpoint_path=/api/chat http_status=%s request_duration_ms=%s response_received=true retry=true",
                        self.model, response.status_code, int((time.monotonic() - started_at) * 1000),
                    )
                    time.sleep(RETRY_BACKOFF_SECONDS)
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Ollama returned a non-object response")
                return payload
            except httpx.TimeoutException as exc:
                logger.warning(
                    "LLM request failed: category=request_timeout provider=ollama model=%s endpoint_path=/api/chat exception_class=%s request_duration_ms=%s request_started=true response_received=false retry=false",
                    self.model, type(exc).__name__, int((time.monotonic() - started_at) * 1000),
                )
                raise LLMProviderError("Ollama request timed out", public_message="The local model took too long to respond. Please retry.") from exc
            except httpx.HTTPStatusError as exc:
                category, summary = _ollama_error_summary(exc.response)
                body_digest = hashlib.sha256(exc.response.content).hexdigest()
                logger.warning(
                    "LLM request failed: category=http_status provider=ollama model=%s endpoint_path=/api/chat http_status=%s request_duration_ms=%s response_received=true provider_error_category=%s provider_error_summary=%r provider_error_body_sha256=%s provider_error_body_bytes=%s request_roles=%s num_predict=%s think=false response_format=%s",
                    self.model, exc.response.status_code, int((time.monotonic() - started_at) * 1000),
                    category, summary, body_digest, len(exc.response.content), roles, max_tokens, response_format,
                )
                if exc.response.status_code in TRANSIENT_HTTP_STATUSES:
                    message = "The local model service is temporarily unavailable. Please retry."
                elif exc.response.status_code == 400:
                    message = f"Ollama rejected the request (HTTP 400): {summary}"
                else:
                    message = f"The local model service returned HTTP {exc.response.status_code}: {summary}"
                raise LLMProviderError(
                    f"Ollama returned HTTP {exc.response.status_code}", public_message=message
                ) from exc
            except httpx.HTTPError as exc:
                transient_transport = isinstance(exc, (httpx.ConnectError, httpx.NetworkError))
                if transient_transport and attempt < MAX_TRANSIENT_RETRIES:
                    logger.warning(
                        "LLM transient transport failure: category=transient_transport provider=ollama model=%s exception_class=%s request_duration_ms=%s response_received=false retry=true",
                        self.model, type(exc).__name__, int((time.monotonic() - started_at) * 1000),
                    )
                    time.sleep(RETRY_BACKOFF_SECONDS)
                    continue
                logger.warning(
                    "LLM request failed: category=transport_error provider=ollama model=%s endpoint_path=/api/chat exception_class=%s request_duration_ms=%s request_started=true response_received=unknown retry=false",
                    self.model, type(exc).__name__, int((time.monotonic() - started_at) * 1000),
                )
                raise LLMProviderError("Ollama request failed", public_message="Cannot connect to the language model service. Check that Ollama is running.") from exc
            except ValueError as exc:
                logger.warning(
                    "LLM response rejected: category=malformed_response_json provider=ollama model=%s endpoint_path=/api/chat exception_class=%s request_duration_ms=%s response_received=true retry=false",
                    self.model, type(exc).__name__, int((time.monotonic() - started_at) * 1000),
                )
                raise LLMProviderError("Ollama returned malformed response data") from exc
        raise LLMProviderError("Ollama request failed")

    def _content(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        try:
            message = payload["message"]
            if not isinstance(message, dict):
                raise TypeError
            content = message.get("content")
            if not isinstance(content, str):
                raise TypeError
            if payload.get("done") is not True:
                raise TypeError
            return payload, content
        except (AttributeError, KeyError, TypeError) as exc:
            self._log_native_response_rejected("unexpected_response_structure", validation_failed=True)
            raise LLMProviderError("Ollama returned an unexpected response structure") from exc

    def generate_text(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        del reasoning_effort  # Native Ollama uses `think`, set in _request_body.
        payload = self._post_chat(self._request_body(system, prompt, max_tokens))
        metadata, content = self._content(payload)
        if metadata.get("done_reason") == "length":
            self._log_native_response_rejected("generation_limit_reached")
            raise LLMProviderError(
                "Ollama response reached its generation limit",
                public_message="The local model response was cut off at its output limit. Please retry with a shorter request.",
            )
        if not content.strip():
            self._log_native_response_rejected("empty_message_content")
            raise LLMProviderError("Ollama returned empty response content")
        return content.strip()

    def generate_json(
        self,
        *,
        system: str,
        prompt: str,
        response_model: type[BaseModel],
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> BaseModel:
        del reasoning_effort  # Native Ollama uses `think`, set in _request_body.
        payload = self._post_chat(self._request_body(system, prompt, max_tokens, response_model))
        metadata, content = self._content(payload)
        if metadata.get("done_reason") == "length":
            self._log_native_response_rejected("generation_limit_reached")
            raise LLMResponseFormatError("Ollama structured response reached its generation limit")
        if not content.strip():
            self._log_native_response_rejected("empty_message_content")
            raise LLMResponseFormatError("Ollama returned empty structured response content")
        try:
            data = json.loads(content)
            return response_model.model_validate(data)
        except json.JSONDecodeError as exc:
            self._log_native_response_rejected("invalid_json_content", validation_failed=True)
            raise LLMResponseFormatError("Ollama returned content that was not valid JSON") from exc
        except ValidationError as exc:
            known_fields = set(response_model.model_fields)
            locations = [
                str(error["loc"][0]) if error["loc"] and error["loc"][0] in known_fields else "unexpected_field"
                for error in exc.errors(include_input=False)
            ]
            self._log_native_response_rejected(
                "response_schema_validation_failed", validation_failed=True,
                fields=",".join(locations) or "unknown",
            )
            raise LLMResponseFormatError("Ollama response did not match the required response schema") from exc

    def _log_native_response_rejected(
        self,
        category: str,
        *,
        validation_failed: bool = False,
        fields: str = "none",
    ) -> None:
        diagnostic = _REQUEST_DIAGNOSTIC.get() or {}
        logger.warning(
            "LLM response rejected: category=%s provider=ollama model=%s endpoint_path=/api/chat request_duration_ms=%s http_status=%s response_received=%s validation_failed=%s fields=%s",
            category, self.model, diagnostic.get("duration_ms", "unknown"),
            diagnostic.get("http_status") or "unknown",
            str(diagnostic.get("response_received", False)).lower(),
            str(validation_failed).lower(), fields,
        )


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
        return OllamaNativeProvider(
            base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
            model=model,
            timeout=timeout,
        )
    raise LLMConfigurationError(f"Unsupported LLM_PROVIDER '{name}'")
