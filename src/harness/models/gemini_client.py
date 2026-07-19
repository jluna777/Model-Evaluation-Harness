"""Gemini judge client (spec §2, §4) -- the third and final client.

Structured output is bound via ``config.response_mime_type`` +
``config.response_json_schema`` on ``client.models.generate_content``.
Verified against the installed SDK (``google-genai`` 2.10.0,
``google/genai/types.py``): ``GenerateContentConfig`` has no
``response_format`` field at all in this version -- only
``response_mime_type``, ``response_schema`` (a Pydantic-model/enum/native
``Schema`` convenience) and ``response_json_schema`` (raw JSON Schema). A raw
JSON Schema dict is passed as ``response_json_schema`` rather than the
Pydantic model class itself: when ``response_schema``/``response_json_schema``
is a dict, ``GenerateContentResponse._from_response`` (``types.py``) only
ever sets ``response.parsed`` from a bare ``json.loads`` and silently
swallows ``JSONDecodeError`` -- it never raises out of the call and never
runs our schema's validators, so this call shape keeps classification of
schema-invalid output entirely in our control, exactly like the Anthropic
and OpenAI clients (T06) calling ``create`` directly instead of
``.parse()``. ``raw`` is ``response.text`` and is validated ourselves via
``schema.model_validate_json``, unconditionally, regardless of whatever the
SDK's own ``response.parsed`` convenience attribute contains -- Gemini's
schema guarantees are documented weaker than Anthropic's/OpenAI's, so this
client never trusts them.

Only the following JSON-Schema keywords are honored by
``response_json_schema`` per the installed SDK's docstring: ``$id``,
``$defs``, ``$ref``, ``$anchor``, ``type``, ``format``, ``title``,
``description``, ``enum`` (strings/numbers), ``items``, ``prefixItems``,
``minItems``, ``maxItems``, ``minimum``, ``maximum``, ``anyOf``, ``oneOf``,
``properties``, ``additionalProperties``, ``required``, plus the
non-standard ``propertyOrdering``. Notably absent: string ``pattern`` --
schemas with pattern constraints (e.g. ``GoldenExpected``) must never be
sent here; ``TicketExtraction``/``JudgeVerdict`` have none.

Gemini has no explicit ``refusal`` stop reason like Anthropic
(``stop_reason == "refusal"``) or OpenAI (a ``type: "refusal"`` content
block). Its closest equivalent is a blocked ``finish_reason`` on the first
candidate (safety/recitation/policy filters) or, when the prompt itself is
blocked before any candidate is produced, ``prompt_feedback.block_reason``
with ``candidates`` left ``None`` entirely -- both are treated as
``failure="refusal"`` here. The alias-drift guard's served version is
``response.model_version``.

SDK-internal retries: unlike anthropic/openai (default ``max_retries=2``,
requiring an explicit override), ``google-genai``'s own default -- when
``http_options.retry_options`` is left unset -- already resolves to
``tenacity.stop_after_attempt(1)`` (``google/genai/_api_client.py``,
``retry_args(None)``), i.e. no SDK-internal retry at all. There is nothing
to explicitly disable; ``retry_transport`` below owns every actual retry
attempt, matching the T06 convention in spirit even though the mechanics
differ.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from harness.models import StructuredResult, Usage
from harness.models.retry import retry_transport

# Finish reasons that indicate the model's output was blocked rather than
# freely generated -- Gemini's equivalent of a refusal. MAX_TOKENS and
# FINISH_REASON_UNSPECIFIED are deliberately excluded: truncated output is a
# schema-invalid candidate failure, not a refusal.
_REFUSAL_FINISH_REASONS = frozenset(
    {
        "SAFETY",
        "RECITATION",
        "LANGUAGE",
        "OTHER",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
        "IMAGE_RECITATION",
        "IMAGE_OTHER",
    }
)


def _is_transport_error(exc: BaseException) -> bool:
    """429 / 5xx / timeout -- the only retryable transport failures (spec §9)."""

    if isinstance(exc, errors.ClientError):
        return exc.code == 429
    if isinstance(exc, errors.ServerError):
        return True
    return isinstance(exc, (httpx.TimeoutException, httpx.ConnectError))


def _refusal_reason(response: types.GenerateContentResponse) -> str | None:
    """Returns a description of why generation was blocked, or ``None`` if
    it was not. Checks the first candidate's ``finish_reason`` first, then
    falls back to prompt-level blocking (no candidates at all)."""

    if response.candidates:
        finish_reason = response.candidates[0].finish_reason
        if finish_reason is not None and finish_reason in _REFUSAL_FINISH_REASONS:
            return f"finish_reason={finish_reason}"
        return None

    feedback = response.prompt_feedback
    if feedback is not None and feedback.block_reason is not None:
        return f"block_reason={feedback.block_reason}"
    return None


class GeminiClient:
    """Judge client for Gemini models via native structured outputs.

    The injected ``client`` is stored as-is: nothing at this class boundary
    inspects or overrides its ``http_options.retry_options``. Retry
    non-compounding is enforced by convention -- construct the client with
    the SDK's defaults, which resolve to no internal retry at all (see the
    module docstring) -- so ``retry_transport`` stays the only retry layer.
    Do not inject a client configured with its own retries.
    """

    def __init__(
        self,
        model: str,
        client: genai.Client,
        *,
        max_attempts: int = 12,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._model = model
        self._client = client
        self._max_attempts = max_attempts
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
        def _call() -> types.GenerateContentResponse:
            return self._client.models.generate_content(
                model=self._model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                    response_json_schema=json_schema,
                ),
            )

        response = _call()

        usage_metadata = response.usage_metadata
        usage = Usage(
            input_tokens=(usage_metadata.prompt_token_count or 0) if usage_metadata else 0,
            output_tokens=(usage_metadata.candidates_token_count or 0) if usage_metadata else 0,
        )
        served_model_version = response.model_version or ""

        refusal_reason = _refusal_reason(response)
        if refusal_reason is not None:
            return StructuredResult(
                output=None,
                failure="refusal",
                raw=response.text or refusal_reason,
                usage=usage,
                served_model_version=served_model_version,
            )

        raw = response.text or ""
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
