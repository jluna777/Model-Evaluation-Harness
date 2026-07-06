import json
from pathlib import Path

import httpx
from google import genai
from google.genai import types

from harness.models.gemini_client import GeminiClient
from harness.models.retry import TransportExhausted
from harness.schema import TicketExtraction

FIXTURES = Path(__file__).parents[2] / "fixtures" / "gemini"
REQUESTED_MODEL = "gemini-3.5-flash"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _client_with_transport(handler, *, retry_attempts: int = 1) -> genai.Client:
    return genai.Client(
        api_key="test-key",
        http_options=types.HttpOptions(
            httpx_client=httpx.Client(transport=httpx.MockTransport(handler)),
            retry_options=types.HttpRetryOptions(attempts=retry_attempts),
        ),
    )


def _json_response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(status_code=status_code, json=payload)


def _no_pattern_key(value: object) -> bool:
    if isinstance(value, dict):
        if "pattern" in value:
            return False
        return all(_no_pattern_key(v) for v in value.values())
    if isinstance(value, list):
        return all(_no_pattern_key(v) for v in value)
    return True


class TestCompleteStructuredSuccess:
    def test_returns_parsed_output_usage_and_raw(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _json_response(200, _load("success.json"))

        client = GeminiClient(model=REQUESTED_MODEL, client=_client_with_transport(handler))
        result = client.complete_structured("extract this", TicketExtraction)

        assert result.failure is None
        assert isinstance(result.output, TicketExtraction)
        assert result.output.order_id == "ORD-77213"
        assert result.usage.input_tokens == 210
        assert result.usage.output_tokens == 68
        assert result.raw
        assert len(calls) == 1


class TestServedModelVersion:
    def test_captured_from_model_version_field_not_requested_model(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(200, _load("success.json"))

        client = GeminiClient(model=REQUESTED_MODEL, client=_client_with_transport(handler))
        result = client.complete_structured("extract this", TicketExtraction)

        assert result.served_model_version == "gemini-3.5-flash-002"
        assert result.served_model_version != REQUESTED_MODEL


class TestOutgoingRequestShape:
    def test_temperature_zero_model_id_and_structured_output_config(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return _json_response(200, _load("success.json"))

        client = GeminiClient(model=REQUESTED_MODEL, client=_client_with_transport(handler))
        client.complete_structured("extract this", TicketExtraction)

        assert REQUESTED_MODEL in captured["url"]
        generation_config = captured["body"]["generationConfig"]
        assert generation_config["temperature"] == 0
        assert generation_config["responseMimeType"] == "application/json"
        assert _no_pattern_key(generation_config["responseJsonSchema"])


class TestRefusal:
    def test_blocked_finish_reason_sets_failure_and_populates_raw(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(200, _load("refusal.json"))

        client = GeminiClient(model=REQUESTED_MODEL, client=_client_with_transport(handler))
        result = client.complete_structured("extract this", TicketExtraction)

        assert result.output is None
        assert result.failure == "refusal"
        assert result.raw


class TestSchemaInvalid:
    def test_non_json_text_is_schema_invalid_and_calls_transport_once(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _json_response(200, _load("invalid_json.json"))

        client = GeminiClient(model=REQUESTED_MODEL, client=_client_with_transport(handler))
        result = client.complete_structured("extract this", TicketExtraction)

        assert result.output is None
        assert result.failure == "schema_invalid"
        assert result.raw
        assert len(calls) == 1


class TestSDKDefaultHasNoRetry:
    def test_client_built_without_explicit_retry_options_still_makes_one_call(self):
        """google-genai's own default (``retry_options`` unset) is already
        "no retry" -- ``retry_args(None)`` resolves to
        ``tenacity.stop_after_attempt(1)`` (verified against the installed
        SDK source). Unlike anthropic/openai (default ``max_retries=2``),
        there is nothing to explicitly disable here; ``retry_transport``
        still owns every actual retry attempt made by ``GeminiClient``."""

        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _json_response(429, _load("rate_limited_error.json"))

        # No retry_options set at all on this SDK client.
        sdk_client = genai.Client(
            api_key="test-key",
            http_options=types.HttpOptions(
                httpx_client=httpx.Client(transport=httpx.MockTransport(handler))
            ),
        )
        client = GeminiClient(model=REQUESTED_MODEL, client=sdk_client, max_attempts=1)

        try:
            client.complete_structured("extract this", TicketExtraction)
        except TransportExhausted:
            pass

        assert len(calls) == 1


class TestRetryOnTransportErrors:
    def test_succeeds_after_429_429_200_in_three_attempts(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) < 3:
                return _json_response(429, _load("rate_limited_error.json"))
            return _json_response(200, _load("success.json"))

        client = GeminiClient(
            model=REQUESTED_MODEL,
            client=_client_with_transport(handler),
            max_attempts=4,
            sleep=lambda _: None,
        )
        result = client.complete_structured("extract this", TicketExtraction)

        assert result.failure is None
        assert len(calls) == 3

    def test_four_consecutive_429s_raises_transport_exhausted(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _json_response(429, _load("rate_limited_error.json"))

        client = GeminiClient(
            model=REQUESTED_MODEL,
            client=_client_with_transport(handler),
            max_attempts=4,
            sleep=lambda _: None,
        )

        try:
            client.complete_structured("extract this", TicketExtraction)
            raised = None
        except TransportExhausted as exc:
            raised = exc

        assert raised is not None
        assert len(calls) == 4

    def test_five_hundred_is_also_retried(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) < 2:
                return _json_response(500, _load("server_error.json"))
            return _json_response(200, _load("success.json"))

        client = GeminiClient(
            model=REQUESTED_MODEL,
            client=_client_with_transport(handler),
            max_attempts=4,
            sleep=lambda _: None,
        )
        result = client.complete_structured("extract this", TicketExtraction)

        assert result.failure is None
        assert len(calls) == 2
