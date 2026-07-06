"""``ModelClient`` protocol and structured-completion result (spec §2, §6).

Exactly three hand-written clients speak this protocol -- Anthropic, OpenAI
(T06), and Gemini (T07) -- no plugin system (constitution §5).
Each wraps its SDK's native structured-output mechanism and returns a
provider-agnostic ``StructuredResult`` so the runner (T08) can treat all
three uniformly.

``raw`` is always populated, including on ``schema_invalid``/``refusal``
failures: candidate output that is schema-invalid or a refusal scores every
field 0 for that replicate, but the raw response is a persisted artifact
that must remain inspectable (spec §6/§7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel

Failure = Literal["schema_invalid", "refusal"]


@dataclass(frozen=True)
class Usage:
    """Token accounting for one candidate call, provider-agnostic."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class StructuredResult:
    """Outcome of one ``complete_structured`` call.

    ``output`` is ``None`` iff ``failure`` is set. ``served_model_version``
    is the provider-reported model identifier read from response metadata --
    the spec §2 alias-drift guard -- and is distinct from the requested,
    pinned model id passed into the client.
    """

    output: BaseModel | None
    failure: Failure | None
    raw: str
    usage: Usage
    served_model_version: str


class ModelClient(Protocol):
    """Thin internal protocol implemented by each candidate/judge client."""

    def complete_structured(self, prompt: str, schema: type[BaseModel]) -> StructuredResult:
        """Render ``prompt`` (already ``PromptTemplate.render(email)``-composed)
        against the provider's native structured-output mechanism, bound to
        ``schema``. Never re-samples a returned response -- ``schema_invalid``
        and ``refusal`` are scored failures, not retry triggers (spec §9)."""
        ...
