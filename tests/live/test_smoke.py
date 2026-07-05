"""Live smoke test for the candidate clients (T06).

Marked ``live`` and excluded from the default test run (default `-m "not
live"` in ``pyproject.toml``). Opt in explicitly:

    uv run pytest -m live tests/live/test_smoke.py

Hits real provider APIs once against a trivial extraction prompt to confirm
the request/response wiring in ``anthropic_client.py`` / ``openai_client.py``
matches the live SDK surface, not just the mocked transports in
``tests/unit/models/``. Skips (rather than fails) when API keys are absent
so CI -- which never selects the ``live`` marker -- and contributors without
keys are unaffected.
"""

from __future__ import annotations

import os

import anthropic
import openai
import pytest

from harness.models.anthropic_client import AnthropicClient
from harness.models.openai_client import OpenAIClient
from harness.schema import TicketExtraction

pytestmark = pytest.mark.live

_PROMPT = (
    "Extract a support ticket from this email.\n\n"
    "From: pat@example.com\n"
    "Subject: Refund for order ORD-55512\n"
    "Body: My replacement headphones arrived broken. Please refund order "
    "ORD-55512 -- I no longer want a replacement.\n"
)


def _require_key(name: str) -> str:
    key = os.environ.get(name)
    if not key:
        pytest.skip(f"{name} not set in environment; skipping live smoke test")
    return key


class TestAnthropicLiveSmoke:
    def test_complete_structured_against_real_api(self):
        api_key = _require_key("ANTHROPIC_API_KEY")
        client = AnthropicClient(
            model="claude-haiku-4-5-20251001",
            client=anthropic.Anthropic(api_key=api_key),
        )

        result = client.complete_structured(_PROMPT, TicketExtraction)

        print("Anthropic live smoke result:", result)
        assert result.raw
        assert result.served_model_version
        assert result.failure in (None, "schema_invalid", "refusal")


class TestOpenAILiveSmoke:
    def test_complete_structured_against_real_api(self):
        api_key = _require_key("OPENAI_API_KEY")
        client = OpenAIClient(
            model="gpt-5.4-mini",
            client=openai.OpenAI(api_key=api_key),
        )

        result = client.complete_structured(_PROMPT, TicketExtraction)

        print("OpenAI live smoke result:", result)
        assert result.raw
        assert result.served_model_version
        assert result.failure in (None, "schema_invalid", "refusal")
