from dataclasses import dataclass, field

from pydantic import BaseModel

from harness.judge.judge import Judge, JudgeVerdict
from harness.judge.rubric import RUBRIC_TEXT
from harness.models import StructuredResult, Usage
from harness.schema import EmailInput

EMAIL = EmailInput(**{"from": "pat@example.com", "subject": "Refund", "body": "Please help."})


@dataclass
class _FakeClient:
    """Test double for the ``ModelClient`` protocol -- records every call."""

    result: StructuredResult
    prompts: list[str] = field(default_factory=list)
    schemas: list[type[BaseModel]] = field(default_factory=list)

    def complete_structured(self, prompt: str, schema: type[BaseModel]) -> StructuredResult:
        self.prompts.append(prompt)
        self.schemas.append(schema)
        return self.result


def _usage() -> Usage:
    return Usage(input_tokens=100, output_tokens=20)


class TestValidVerdictParses:
    def test_pass_verdict_populates_rationale_and_raw(self):
        output = JudgeVerdict(verdict="pass", rationale="Same issue and action.")
        fake = _FakeClient(
            result=StructuredResult(
                output=output,
                failure=None,
                raw='{"verdict": "pass", "rationale": "Same issue and action."}',
                usage=_usage(),
                served_model_version="gemini-3.5-flash-002",
            )
        )
        judge = Judge(fake)

        result = judge.judge_field(EMAIL, "issue_summary", "reference text", "candidate text")

        assert result.verdict == "pass"
        assert result.error is None
        assert result.rationale == "Same issue and action."
        assert result.raw

    def test_fail_verdict_populates_rationale_and_raw(self):
        output = JudgeVerdict(verdict="fail", rationale="Adds an unsupported claim.")
        fake = _FakeClient(
            result=StructuredResult(
                output=output,
                failure=None,
                raw='{"verdict": "fail", "rationale": "Adds an unsupported claim."}',
                usage=_usage(),
                served_model_version="gemini-3.5-flash-002",
            )
        )
        judge = Judge(fake)

        result = judge.judge_field(EMAIL, "requested_action", "reference text", "candidate text")

        assert result.verdict == "fail"
        assert result.error is None
        assert result.rationale == "Adds an unsupported claim."


class TestJudgeErrorNeverBecomesFail:
    def test_schema_invalid_output_is_error_not_fail(self):
        fake = _FakeClient(
            result=StructuredResult(
                output=None,
                failure="schema_invalid",
                raw="not valid json",
                usage=_usage(),
                served_model_version="gemini-3.5-flash-002",
            )
        )
        judge = Judge(fake)

        result = judge.judge_field(EMAIL, "issue_summary", "reference text", "candidate text")

        assert result.verdict is None
        assert result.verdict != "fail"
        assert result.error is not None
        assert result.raw == "not valid json"

    def test_refusal_output_is_error_not_fail(self):
        fake = _FakeClient(
            result=StructuredResult(
                output=None,
                failure="refusal",
                raw="finish_reason=SAFETY",
                usage=_usage(),
                served_model_version="gemini-3.5-flash-002",
            )
        )
        judge = Judge(fake)

        result = judge.judge_field(EMAIL, "requested_action", "reference text", "candidate text")

        assert result.verdict is None
        assert result.verdict != "fail"
        assert result.error is not None
        assert result.raw == "finish_reason=SAFETY"

    def test_error_reasons_are_distinguishable(self):
        schema_invalid = _FakeClient(
            result=StructuredResult(
                output=None,
                failure="schema_invalid",
                raw="x",
                usage=_usage(),
                served_model_version="v",
            )
        )
        refusal = _FakeClient(
            result=StructuredResult(
                output=None, failure="refusal", raw="y", usage=_usage(), served_model_version="v"
            )
        )

        schema_invalid_result = Judge(schema_invalid).judge_field(EMAIL, "issue_summary", "r", "c")
        refusal_result = Judge(refusal).judge_field(EMAIL, "issue_summary", "r", "c")

        assert schema_invalid_result.error != refusal_result.error


class TestOneCallPerField:
    def test_judge_field_issues_exactly_one_model_call(self):
        output = JudgeVerdict(verdict="pass", rationale="ok")
        fake = _FakeClient(
            result=StructuredResult(
                output=output,
                failure=None,
                raw="{}",
                usage=_usage(),
                served_model_version="v",
            )
        )
        judge = Judge(fake)

        judge.judge_field(EMAIL, "issue_summary", "reference text", "candidate text")

        assert len(fake.prompts) == 1
        assert len(fake.schemas) == 1
        assert fake.schemas[0] is JudgeVerdict


class TestPromptContent:
    def test_prompt_includes_rubric_reference_and_candidate_value(self):
        output = JudgeVerdict(verdict="pass", rationale="ok")
        fake = _FakeClient(
            result=StructuredResult(
                output=output,
                failure=None,
                raw="{}",
                usage=_usage(),
                served_model_version="v",
            )
        )
        judge = Judge(fake)

        judge.judge_field(
            EMAIL, "issue_summary", "the specific reference value", "the specific candidate value"
        )

        prompt = fake.prompts[0]
        assert RUBRIC_TEXT in prompt
        assert "the specific reference value" in prompt
        assert "the specific candidate value" in prompt
        assert "issue_summary" in prompt
        assert EMAIL.subject in prompt
