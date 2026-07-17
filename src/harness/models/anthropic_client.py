"""Anthropic candidate client (spec §1, §2).

Structured output is bound via ``output_config.format`` (``json_schema``) on
``client.messages.create``. Verified against the installed SDK (anthropic
0.116.0): ``MessageCreateParamsBase`` (``types/message_create_params.py``)
has no wire-level ``output_format`` field at all -- only ``output_config``.
An ``output_format=<pydantic model>`` convenience kwarg exists solely on the
higher-level ``.stream()``/``.parse()`` helpers, which translate it into
``output_config["format"]`` before sending; it is sugar, not a distinct or
deprecated API surface, so ``create`` + ``output_config`` is the direct,
current mechanism for both call shapes.

``create`` is called directly rather than the SDK's ``messages.parse``
helper: ``parse`` eagerly raises a ``pydantic.ValidationError`` out of
``client.messages.parse(...)`` itself when the model's JSON doesn't
validate, which would make a schema-invalid *candidate* failure
indistinguishable from a bug in our own call. Validating the returned text
ourselves keeps that classification inside our control (spec §6):
schema-invalid output and refusals are scored failures, not exceptions, and
``raw`` stays populated either way.

Refusals surface as ``stop_reason == "refusal"`` (``anthropic.types.
StopReason``); the alias-drift guard's served version is ``response.model``.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

import anthropic
from pydantic import BaseModel, ValidationError

from harness.models import StructuredResult, Usage
from harness.models.retry import retry_transport


def _is_transport_error(exc: BaseException) -> bool:
    """429 / 5xx / timeout -- the only retryable transport failures (spec §9)."""

    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return isinstance(exc, anthropic.APIConnectionError)


class AnthropicClient:
    """Candidate client for Claude models via native structured outputs."""

    def __init__(
        self,
        model: str,
        client: anthropic.Anthropic,
        *,
        max_attempts: int = 12,
        max_tokens: int = 1024,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._model = model
        # Disable SDK-internal retries; retry_transport decorator owns ALL retry behavior
        # (config cap + jittered backoff).
        self._client = client.with_options(max_retries=0)
        self._max_attempts = max_attempts
        self._max_tokens = max_tokens
        self._sleep = sleep
        self._jitter = jitter

    def complete_structured(self, prompt: str, schema: type[BaseModel]) -> StructuredResult:
        json_schema = schema.model_json_schema()

        @retry_transport(
            max_attempts=self._max_attempts,
            is_transport_error=_is_transport_error,
            sleep=self._sleep,
            jitter=self._jitter,
        )
        def _call() -> anthropic.types.Message:
            return self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
                output_config={"format": {"type": "json_schema", "schema": json_schema}},
            )

        response = _call()

        raw = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        usage = Usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        served_model_version = str(response.model)

        if response.stop_reason == "refusal":
            return StructuredResult(
                output=None,
                failure="refusal",
                raw=raw,
                usage=usage,
                served_model_version=served_model_version,
            )

        try:
            output = schema.model_validate_json(raw)
        except ValidationError:
            return StructuredResult(
                output=None,
                failure="schema_invalid",
                raw=raw,
                usage=usage,
                served_model_version=served_model_version,
            )

        return StructuredResult(
            output=output,
            failure=None,
            raw=raw,
            usage=usage,
            served_model_version=served_model_version,
        )
