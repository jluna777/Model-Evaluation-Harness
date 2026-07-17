"""OpenAI candidate client (spec §1, §2).

Structured output is bound via the Responses API's strict ``json_schema``
text format (``text.format``) on ``client.responses.create``. ``create`` is
called directly rather than ``client.responses.parse``/``chat.completions.
parse`` for the same reason as the Anthropic client: those helpers raise a
``pydantic.ValidationError`` out of the call itself on invalid JSON, which
would make a schema-invalid *candidate* failure indistinguishable from a bug
in our own code. Validating ``response.output_text`` ourselves keeps that
classification inside our control (spec §6): schema-invalid output and
refusals are scored failures, not exceptions, and ``raw`` stays populated
either way.

A refusal surfaces as a ``type: "refusal"`` content block inside an output
message (``openai.types.responses.ResponseOutputRefusal``); the alias-drift
guard's served version is ``response.model``.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

import openai
from pydantic import BaseModel, ValidationError

from harness.models import StructuredResult, Usage
from harness.models.retry import retry_transport


def _is_transport_error(exc: BaseException) -> bool:
    """429 / 5xx / timeout -- the only retryable transport failures (spec §9)."""

    if isinstance(exc, openai.APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return isinstance(exc, openai.APIConnectionError)


def _extract_refusal(response: openai.types.responses.Response) -> str | None:
    for item in response.output:
        if getattr(item, "type", None) != "message":
            continue
        for content in item.content:
            if getattr(content, "type", None) == "refusal":
                return content.refusal
    return None


class OpenAIClient:
    """Candidate client for GPT models via the Responses API's strict structured outputs."""

    def __init__(
        self,
        model: str,
        client: openai.OpenAI,
        *,
        max_attempts: int = 12,
        schema_name: str = "ticket_extraction",
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._model = model
        # Disable SDK-internal retries; retry_transport decorator owns ALL retry behavior
        # (config cap + jittered backoff).
        self._client = client.with_options(max_retries=0)
        self._max_attempts = max_attempts
        self._schema_name = schema_name
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
        def _call() -> openai.types.responses.Response:
            return self._client.responses.create(
                model=self._model,
                temperature=0,
                input=prompt,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": self._schema_name,
                        "schema": json_schema,
                        "strict": True,
                    }
                },
            )

        response = _call()

        usage = response.usage
        usage_result = Usage(
            input_tokens=usage.input_tokens if usage is not None else 0,
            output_tokens=usage.output_tokens if usage is not None else 0,
        )
        served_model_version = str(response.model)

        refusal_text = _extract_refusal(response)
        if refusal_text is not None:
            return StructuredResult(
                output=None,
                failure="refusal",
                raw=refusal_text,
                usage=usage_result,
                served_model_version=served_model_version,
            )

        raw = response.output_text
        try:
            output = schema.model_validate_json(raw)
        except ValidationError:
            return StructuredResult(
                output=None,
                failure="schema_invalid",
                raw=raw,
                usage=usage_result,
                served_model_version=served_model_version,
            )

        return StructuredResult(
            output=output,
            failure=None,
            raw=raw,
            usage=usage_result,
            served_model_version=served_model_version,
        )
