"""``judge_field()`` -- pointwise, reference-guided LLM judging (spec §4).

Each free-text field of each candidate output is judged in its own call:
inputs are the email, the field name, the field's reference value, and the
candidate's value; output is a binary verdict plus a one-sentence rationale
(``JudgeVerdict``, Pydantic-validated -- spec §4). ``Judge`` wraps any
``ModelClient`` (in production, ``GeminiClient``; in tests, a fake) so the
one-call-per-field contract and the error-vs-fail split can be tested
without a real transport.

**Error-vs-fail line (spec §7, binding):** ``ModelClient.complete_structured``
already separates "the model returned a response we can't use"
(``failure="schema_invalid"`` / ``"refusal"``) from "the model successfully
returned a validated verdict". ``judge_field`` preserves that split exactly:
a judge error surfaces as ``verdict=None, error=<reason>``, never as
``verdict="fail"``. Downstream (T08 rows, T16 gate) must exclude judge
errors from scoring rather than counting them as candidate regressions --
coercing an error to ``"fail"`` would silently violate that.

A ``TransportExhausted`` raised by the underlying client (retries exhausted)
is deliberately not caught here: per spec §6, that is a measurement error
that aborts the run, not a per-field outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from harness.judge.rubric import FEW_SHOTS, PROMPT_PREAMBLE, RUBRIC_TEXT
from harness.models import ModelClient
from harness.schema import EmailInput


class JudgeVerdict(BaseModel):
    """Wire schema for one judge call's structured output (spec §4)."""

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["pass", "fail"]
    rationale: str


@dataclass(frozen=True)
class JudgeResult:
    """Outcome of one ``judge_field`` call.

    ``verdict`` is ``None`` iff ``error`` is set -- a judge error (refusal or
    schema-invalid output) is never coerced to ``"fail"``. ``raw`` is always
    populated so the underlying model response stays inspectable regardless
    of outcome.
    """

    verdict: Literal["pass", "fail"] | None
    error: str | None
    rationale: str | None
    raw: str


def _render_few_shots() -> str:
    blocks = []
    for fs in FEW_SHOTS:
        blocks.append(
            "Example:\n"
            f"Reference: {fs.reference}\n"
            f"Candidate: {fs.candidate_value}\n"
            f"Verdict: {fs.verdict}\n"
            f"Critique: {fs.critique}"
        )
    return "\n\n".join(blocks)


def _render_prompt(
    email: EmailInput, field_name: str, reference: str, candidate_value: str
) -> str:
    return (
        f"{PROMPT_PREAMBLE}\n\n"
        f"Rubric: {RUBRIC_TEXT}\n\n"
        f"{_render_few_shots()}\n\n"
        "Now judge this case.\n"
        f"Email from: {email.from_}\n"
        f"Email subject: {email.subject}\n"
        f"Email body:\n{email.body}\n\n"
        f"Field being judged: {field_name}\n"
        f"Reference value: {reference}\n"
        f"Candidate value: {candidate_value}\n"
    )


class Judge:
    """Binds a ``ModelClient`` (the Gemini judge, in production) to the
    pinned rubric/few-shots and exposes one-field-per-call judging."""

    def __init__(self, client: ModelClient) -> None:
        self._client = client

    def judge_field(
        self,
        email: EmailInput,
        field_name: str,
        reference: str,
        candidate_value: str,
    ) -> JudgeResult:
        """Judge one candidate field against its reference. Issues exactly
        one model call (spec §4: one field per call)."""

        prompt = _render_prompt(email, field_name, reference, candidate_value)
        result = self._client.complete_structured(prompt, JudgeVerdict)

        if result.failure is not None:
            return JudgeResult(
                verdict=None,
                error=result.failure,
                rationale=None,
                raw=result.raw,
            )

        output = result.output
        if not isinstance(output, JudgeVerdict):
            # StructuredResult's contract (models/__init__.py) is output is None iff
            # failure is set; failure is None here, so this is a ModelClient bug, not a
            # judge or candidate outcome.
            raise AssertionError(
                f"ModelClient.complete_structured returned failure=None with output={output!r}"
            )
        return JudgeResult(
            verdict=output.verdict,
            error=None,
            rationale=output.rationale,
            raw=result.raw,
        )
