import json
from pathlib import Path

import httpx
import openai

from harness.models.openai_client import OpenAIClient
from harness.models.retry import TransportExhausted
from harness.schema import TicketExtraction

FIXTURES = Path(__file__).parents[2] / "fixtures" / "openai"
REQUESTED_MODEL = "gpt-5.4-mini"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _client_with_transport(handler) -> openai.OpenAI:
    return openai.OpenAI(
        api_key="test-key",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
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

        client = OpenAIClient(model=REQUESTED_MODEL, client=_client_with_transport(handler))
        result = client.complete_structured("extract this", TicketExtraction)

        assert result.failure is None
        assert isinstance(result.output, TicketExtraction)
        assert result.output.order_id == "ORD-11223"
        assert result.usage.input_tokens == 300
        assert result.usage.output_tokens == 80
        assert result.raw
        assert len(calls) == 1


class TestServedModelVersion:
    def test_captured_from_response_model_field_not_requested_model(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(200, _load("success.json"))

        client = OpenAIClient(model=REQUESTED_MODEL, client=_client_with_transport(handler))
        result = client.complete_structured("extract this", TicketExtraction)

        assert result.served_model_version == "gpt-5.4-mini-2026-02-01"
        assert result.served_model_version != REQUESTED_MODEL


class TestOutgoingRequestShape:
    def test_temperature_zero_strict_json_schema_no_pattern(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return _json_response(200, _load("success.json"))

        client = OpenAIClient(model=REQUESTED_MODEL, client=_client_with_transport(handler))
        client.complete_structured("extract this", TicketExtraction)

        body = captured["body"]
        assert body["temperature"] == 0
        assert body["model"] == REQUESTED_MODEL
        text_format = body["text"]["format"]
        assert text_format["type"] == "json_schema"
        assert text_format["strict"] is True
        assert _no_pattern_key(text_format["schema"])


class TestRefusal:
    def test_refusal_response_sets_failure_and_populates_raw(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(200, _load("refusal.json"))

        client = OpenAIClient(model=REQUESTED_MODEL, client=_client_with_transport(handler))
        result = client.complete_structured("extract this", TicketExtraction)

        assert result.output is None
        assert result.failure == "refusal"
        assert result.raw


class TestSchemaInvalid:
    def test_invalid_json_is_schema_invalid_and_calls_transport_once(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _json_response(200, _load("invalid_json.json"))

        client = OpenAIClient(model=REQUESTED_MODEL, client=_client_with_transport(handler))
        result = client.complete_structured("extract this", TicketExtraction)

        assert result.output is None
        assert result.failure == "schema_invalid"
        assert result.raw
        assert len(calls) == 1


class TestRetryOnTransportErrors:
    def test_succeeds_after_429_429_200_in_three_attempts(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) < 3:
                return _json_response(429, _load("rate_limited_error.json"))
            return _json_response(200, _load("success.json"))

        client = OpenAIClient(
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

        client = OpenAIClient(
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

        client = OpenAIClient(
            model=REQUESTED_MODEL,
            client=_client_with_transport(handler),
            max_attempts=4,
            sleep=lambda _: None,
        )
        result = client.complete_structured("extract this", TicketExtraction)

        assert result.failure is None
        assert len(calls) == 2
