"""Tests for the CI gate decision rule and ``eval gate`` CLI (T16, spec §7).

Two layers, deliberately separated:

- **Pure decision-rule / data-extraction tests** (``TestDecideCandidateResult*``,
  ``TestNominalPairedDeltas*``, ``TestAdversarialCompositeDelta*``,
  ``TestJudgeErrorRate*``, ``TestEvaluateGate*``) exercise ``harness.gate.gate``
  directly with hand-built ``BaselineFile``/``RunArtifact`` fixtures (mirrors
  ``tests/unit/gate/test_baseline.py``'s style) or, where the ticket's fixture
  anchors need EXACT point-value control (the "12-point regression on 20
  items", "5 items x 15pt -> p=0.031" anchors), with hand-built delta lists
  fed straight into ``decide_candidate_result`` -- sidestepping the
  discrete-field-scoring granularity of real ``RunRow`` composites (100/7
  points per field flip under FULL_7), which cannot hit those exact values.
- **CLI surface tests** (``TestGateCli*``) use ``typer.testing.CliRunner``
  end-to-end, with fake candidate/judge clients (mirrors
  ``tests/unit/test_cli.py``'s conventions), to prove the command wiring:
  flag handling, exit codes, error messages, the DEMO MODE banner, and
  ``--update-baseline``'s guardrail-refusal behavior.

No live API calls anywhere in this module.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel
from typer.testing import CliRunner

import harness.cli as cli
from harness.cli import app
from harness.config import Config, load_config
from harness.gate import gate
from harness.gate.baseline import (
    BaselineFile,
    FingerprintComponents,
    _write_baseline,
    generate_baseline,
    load_baseline,
)
from harness.judge.judge import JudgeVerdict
from harness.models import StructuredResult, Usage
from harness.prompts import DEGRADED_DEMO_PROMPT, EXTRACTION_PROMPT
from harness.runner import ModelKey, RunArtifact, RunDir, RunRow, load_run
from harness.schema import (
    Certificate,
    EmailInput,
    GoldenExpected,
    GoldenItem,
    GoldenMeta,
    TicketExtraction,
)
from harness.scoring.composite import JUDGED_FIELDS, CompositeMode

DEFAULT_CONFIG_PATH = Path(__file__).parents[3] / "configs" / "default.yaml"

runner = CliRunner()


def _config() -> Config:
    return load_config(DEFAULT_CONFIG_PATH)


CONFIG = _config()


# --------------------------------------------------------------------------
# Shared fixtures/helpers (mirrors test_baseline.py's / test_cli.py's style).
# --------------------------------------------------------------------------


def make_item(item_id: str, *, slice_: str = "nominal") -> GoldenItem:
    email_kwargs = {"from": f"{item_id}@example.com", "subject": "Subject", "body": "Body text."}
    return GoldenItem(
        id=item_id,
        email=EmailInput(**email_kwargs),
        expected=GoldenExpected(
            category="billing",
            priority="normal",
            customer_name="Jane Doe",
            order_id=None,
            product_name=None,
            issue_summary="Customer's order arrived damaged.",
            requested_action="Customer wants a refund.",
        ),
        meta=GoldenMeta(
            slice=slice_,
            categories=["billing"],
            difficulty=1,
            generator="gpt-4",
            edited=False,
            notes="",
        ),
    )


def success_result(served_model_version: str = "candidate-v1") -> StructuredResult:
    output = TicketExtraction(
        category="billing",
        priority="normal",
        customer_name="Jane Doe",
        order_id=None,
        product_name=None,
        issue_summary="Customer's order arrived damaged.",
        requested_action="Customer wants a refund.",
    )
    return StructuredResult(
        output=output,
        failure=None,
        raw=output.model_dump_json(),
        usage=Usage(input_tokens=50, output_tokens=20),
        served_model_version=served_model_version,
    )


def wrong_result() -> StructuredResult:
    """A structurally valid but deliberately WRONG candidate output --
    disagrees on every deterministic field with ``make_item``'s expected
    values (used to induce a controlled regression against a fake judge that
    always passes issue_summary/requested_action)."""

    output = TicketExtraction(
        category="other",
        priority="low",
        customer_name=None,
        order_id=None,
        product_name=None,
        issue_summary="Wrong summary.",
        requested_action="Wrong action.",
    )
    return StructuredResult(
        output=output,
        failure=None,
        raw=output.model_dump_json(),
        usage=Usage(input_tokens=50, output_tokens=20),
        served_model_version="candidate-v1",
    )


def judge_pass_result() -> StructuredResult:
    output = JudgeVerdict(verdict="pass", rationale="Same issue and action.")
    return StructuredResult(
        output=output,
        failure=None,
        raw=output.model_dump_json(),
        usage=Usage(input_tokens=5, output_tokens=5),
        served_model_version="judge-v1",
    )


def judge_error_result() -> StructuredResult:
    return StructuredResult(
        output=None,
        failure="refusal",
        raw="refused",
        usage=Usage(input_tokens=5, output_tokens=0),
        served_model_version="judge-v1",
    )


@dataclass
class FakeModelClient:
    """Test double for the ``ModelClient`` protocol -- thread-safe call
    recording, mirroring ``tests/unit/test_cli.py``'s own fake."""

    make_result: Callable[[int, str, type[BaseModel]], StructuredResult]
    calls: list[tuple[str, type]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def complete_structured(self, prompt: str, schema: type[BaseModel]) -> StructuredResult:
        with self._lock:
            idx = len(self.calls)
            self.calls.append((prompt, schema))
        return self.make_result(idx, prompt, schema)

    @property
    def call_count(self) -> int:
        return len(self.calls)


def _model_key(label: str = "a", candidate=None, judge=None) -> ModelKey:
    return ModelKey(
        label=label,
        candidate_client=candidate or FakeModelClient(make_result=lambda *a: success_result()),
        judge_client=judge or FakeModelClient(make_result=lambda *a: judge_pass_result()),
    )


def _all_fields(value: int | None) -> dict[str, int | None]:
    return {
        "category": value,
        "priority": value,
        "customer_name": value,
        "order_id": value,
        "product_name": value,
        "issue_summary": value,
        "requested_action": value,
    }


def _row(
    item_id: str,
    replicate: int,
    field_scores: dict[str, int | None],
    *,
    candidate_failed: bool = False,
) -> RunRow:
    """Builds a ``RunRow`` honoring runner.py's binding convention: a judge
    call was attempted for a field iff ``raw_judge[field]`` is not ``None``
    (regardless of whether that call succeeded or errored); ``raw_judge`` is
    ``None`` for every judged field only when the candidate itself failed
    (schema-invalid/refusal) and no judge call was ever made. This precision
    matters for ``gate.judge_error_rate``'s denominator, unlike
    ``test_baseline.py``'s simpler ``_row`` helper which doesn't need it."""

    if candidate_failed:
        raw_judge: dict[str, str | None] = dict.fromkeys(JUDGED_FIELDS)
        rationales: dict[str, str | None] = dict.fromkeys(JUDGED_FIELDS)
    else:
        raw_judge = {f: '{"verdict": "pass"}' for f in JUDGED_FIELDS}
        rationales = {f: ("ok" if field_scores.get(f) is not None else None) for f in JUDGED_FIELDS}
    return RunRow(
        item_id=item_id,
        replicate=replicate,
        raw_output="{}",
        raw_judge=raw_judge,
        field_scores=field_scores,
        usage={"input_tokens": 1, "output_tokens": 1},
        served_model_version="candidate-v1",
        judge_rationales=rationales,
        judge_usage=None,
    )


def _all_rows(items: list[GoldenItem], k: int, field_scores_fn=None) -> list[RunRow]:
    field_scores_fn = field_scores_fn or (lambda item_id, replicate: _all_fields(1))
    rows = []
    for item in items:
        for r in range(k):
            rows.append(_row(item.id, r, field_scores_fn(item.id, r)))
    return rows


def _make_baseline(
    items: list[GoldenItem],
    rows: list[RunRow],
    *,
    label: str = "a",
    composite_mode: CompositeMode = CompositeMode.FULL_7,
    calibration_verdict: str = "adequate",
    served_versions: dict[str, str] | None = None,
    judge_version: str = "judge-version-hash",
    prompt_version: int = 1,
    dataset_version: int = 1,
    adversarial_noise_floor_se: float = 0.0,
) -> BaselineFile:
    served_versions = served_versions or {f"candidate_{label}": "candidate-v1", "judge": "judge-v1"}
    components = FingerprintComponents(
        prompt_version=prompt_version,
        dataset_version=dataset_version,
        served_versions=served_versions,
        judge_version=judge_version,
        composite_mode=str(composite_mode),
        calibration_verdict=calibration_verdict,
    )
    return BaselineFile(
        schema_version=1,
        label=label,
        k_baseline=6,
        items=tuple(items),
        rows=tuple(rows),
        fingerprint="baseline-fingerprint",
        fingerprint_components=components,
        adversarial_noise_floor_se=adversarial_noise_floor_se,
        created_at="2026-07-04T00:00:00+00:00",
    )


def _make_run_artifact(
    items: list[GoldenItem],
    rows: list[RunRow],
    *,
    label: str = "a",
    k: int = 3,
    prompt_version: int = 1,
    dataset_version: int = 1,
    served_versions: dict[str, str] | None = None,
    judge_version: str = "judge-version-hash",
    completed: bool = True,
    untraced: bool = False,
) -> RunArtifact:
    served_versions = served_versions or {f"candidate_{label}": "candidate-v1", "judge": "judge-v1"}
    return RunArtifact(
        run_dir=RunDir(path=Path(f"fake-run-{label}")),
        model_key=label,
        k=k,
        prompt_version=prompt_version,
        dataset_version=dataset_version,
        items=tuple(items),
        rows=tuple(rows),
        served_versions=served_versions,
        judge_version=judge_version,
        fingerprint="run-fingerprint",
        completed=completed,
        untraced=untraced,
    )


def _certificate(verdict: str = "adequate") -> Certificate:
    return Certificate(
        judge_version="judge-version-hash",
        overall_kappa=0.7,
        kappa_ci=(0.5, 0.85),
        per_candidate_kappa={"a": 0.7, "b": 0.7},
        verdict=verdict,
        ceiling_kappa=None,
        label_file_hash="deadbeef",
        date="2026-06-01",
    )


ZERO_USAGE = {"input_tokens": 0, "output_tokens": 0}


def _decide(label, deltas, *, judge_error_excluded=0, adversarial_delta=0.0, config=None):
    return gate.decide_candidate_result(
        label,
        deltas,
        judge_error_excluded=judge_error_excluded,
        adversarial_delta=adversarial_delta,
        usage_candidate=ZERO_USAGE,
        usage_judge=ZERO_USAGE,
        untraced=False,
        config=config or CONFIG,
    )


# --------------------------------------------------------------------------
# Pure decision rule: hand-built deltas, exact point-value control (ticket's
# core statistical fixture anchors).
# --------------------------------------------------------------------------


class TestDecideCandidateResultUnchanged:
    def test_all_zero_deltas_passes(self):
        result = _decide("a", [0.0] * 32)

        assert result.verdict == "pass"
        assert result.delta == pytest.approx(0.0)


class TestDecideCandidateResultTwelvePointRegression:
    def test_twelve_points_on_twenty_of_thirtytwo_items_fails(self):
        deltas = [-12.0] * 20 + [0.0] * 12

        result = _decide("a", deltas)

        assert result.m_nonzero == 20
        assert result.delta == pytest.approx(-240.0 / 32.0)
        assert result.verdict == "fail"

    def test_twelve_points_on_three_of_thirtytwo_items_passes_rejection_impossible(self):
        deltas = [-12.0] * 3 + [0.0] * 29

        result = _decide("a", deltas)

        assert result.m_nonzero == 3
        assert result.min_attainable_p == pytest.approx(2.0**-3)
        assert result.p_value >= 0.05  # can't reject at m<=4 regardless of magnitude
        assert result.verdict == "pass"


class TestDecideCandidateResultFiveItemSparseCase:
    """Spec §7 errata 2026-07-04: at m=5, rejection requires all five
    nonzero deltas to be regressions (min p = 0.031)."""

    def test_five_items_fifteen_points_all_negative_fails_at_p_0_031(self):
        deltas = [-15.0] * 5 + [0.0] * 27

        result = _decide("a", deltas)

        assert result.m_nonzero == 5
        assert result.p_value == pytest.approx(1.0 / 32.0)
        assert result.delta == pytest.approx(-75.0 / 32.0)
        assert -result.delta > CONFIG.gate.margin
        assert result.verdict == "fail"

    def test_five_items_eight_points_passes_despite_significant_p(self):
        deltas = [-8.0] * 5 + [0.0] * 27

        result = _decide("a", deltas)

        assert result.m_nonzero == 5
        assert result.p_value == pytest.approx(1.0 / 32.0)  # still significant
        assert -result.delta < CONFIG.gate.margin  # but margin condition fails
        assert result.verdict == "pass"


class TestDecideCandidateResultAdversarialGuardrail:
    def test_fifteen_point_adversarial_drop_fails_via_guardrail_alone(self):
        result = _decide("a", [0.0] * 32, adversarial_delta=-15.0)

        assert result.adversarial_guardrail_tripped is True
        assert result.verdict == "fail"

    def test_adversarial_delta_always_populated_when_not_tripped(self):
        result = _decide("a", [0.0] * 32, adversarial_delta=-2.0)

        assert result.adversarial_delta == pytest.approx(-2.0)
        assert result.adversarial_guardrail_tripped is False
        assert result.verdict == "pass"

    def test_boundary_exactly_ten_points_trips(self):
        result = _decide("a", [0.0] * 32, adversarial_delta=-10.0)

        assert result.adversarial_guardrail_tripped is True


class TestDecideCandidateResultJudgeErrorExclusionPassthrough:
    def test_exclusion_count_carried_verbatim_and_does_not_affect_verdict(self):
        result = _decide("a", [0.0] * 30, judge_error_excluded=2)

        assert result.judge_error_excluded == 2
        assert result.verdict == "pass"


# --------------------------------------------------------------------------
# nominal_paired_deltas: item-level judge-error exclusion + delta arithmetic.
# --------------------------------------------------------------------------


class TestNominalPairedDeltas:
    def test_identical_baseline_and_run_gives_zero_deltas(self):
        items = [make_item("nom-0"), make_item("nom-1"), make_item("adv-0", slice_="adversarial")]
        baseline = _make_baseline(items, _all_rows(items, 6))
        run = _make_run_artifact(items, _all_rows(items, 3))

        deltas, excluded = gate.nominal_paired_deltas(baseline, run, CompositeMode.FULL_7)

        assert deltas == [0.0, 0.0]
        assert excluded == 0

    def test_run_side_full_failure_produces_full_negative_delta(self):
        items = [make_item("nom-0")]
        baseline = _make_baseline(items, _all_rows(items, 6, lambda i, r: _all_fields(1)))
        run = _make_run_artifact(items, _all_rows(items, 3, lambda i, r: _all_fields(0)))

        deltas, excluded = gate.nominal_paired_deltas(baseline, run, CompositeMode.FULL_7)

        assert deltas == pytest.approx([-100.0])
        assert excluded == 0

    def test_judge_error_on_run_side_excludes_item_and_counts_it(self):
        items = [make_item("nom-0"), make_item("nom-1")]
        baseline = _make_baseline(items, _all_rows(items, 6))

        def scores(item_id: str, replicate: int) -> dict[str, int | None]:
            if item_id == "nom-0":
                return {**_all_fields(1), "issue_summary": None}
            return _all_fields(1)

        run = _make_run_artifact(items, _all_rows(items, 3, scores))

        deltas, excluded = gate.nominal_paired_deltas(baseline, run, CompositeMode.FULL_7)

        assert excluded == 1
        assert deltas == [0.0]  # only nom-1 survives, unchanged

    def test_two_judge_error_items_excluded_count_is_two(self):
        items = [make_item(f"nom-{i}") for i in range(4)]
        baseline = _make_baseline(items, _all_rows(items, 6))

        def scores(item_id: str, replicate: int) -> dict[str, int | None]:
            if item_id in ("nom-0", "nom-1"):
                return {**_all_fields(1), "requested_action": None}
            return _all_fields(1)

        run = _make_run_artifact(items, _all_rows(items, 3, scores))

        deltas, excluded = gate.nominal_paired_deltas(baseline, run, CompositeMode.FULL_7)

        assert excluded == 2
        assert deltas == [0.0, 0.0]

    def test_deterministic_5_mode_never_excludes_for_judge_errors(self):
        items = [make_item("nom-0")]
        baseline = _make_baseline(
            items, _all_rows(items, 6), composite_mode=CompositeMode.DETERMINISTIC_5
        )
        run = _make_run_artifact(
            items, _all_rows(items, 3, lambda i, r: {**_all_fields(1), "issue_summary": None})
        )

        deltas, excluded = gate.nominal_paired_deltas(baseline, run, CompositeMode.DETERMINISTIC_5)

        assert excluded == 0
        assert deltas == [0.0]

    def test_adversarial_items_are_not_included_in_nominal_deltas(self):
        items = [make_item("nom-0"), make_item("adv-0", slice_="adversarial")]
        baseline = _make_baseline(items, _all_rows(items, 6))

        def scores(item_id: str, replicate: int) -> dict[str, int | None]:
            return _all_fields(0) if item_id == "adv-0" else _all_fields(1)

        run = _make_run_artifact(items, _all_rows(items, 3, scores))

        deltas, excluded = gate.nominal_paired_deltas(baseline, run, CompositeMode.FULL_7)

        assert deltas == [0.0]  # the adversarial item's regression must never appear here


# --------------------------------------------------------------------------
# adversarial_composite_delta.
# --------------------------------------------------------------------------


class TestAdversarialCompositeDelta:
    def test_no_change_gives_zero(self):
        items = [make_item("adv-0", slice_="adversarial")]
        baseline = _make_baseline(items, _all_rows(items, 6))
        run = _make_run_artifact(items, _all_rows(items, 3))

        delta = gate.adversarial_composite_delta(baseline, run, CompositeMode.FULL_7)

        assert delta == pytest.approx(0.0)

    def test_full_regression_gives_minus_100(self):
        items = [make_item("adv-0", slice_="adversarial")]
        baseline = _make_baseline(items, _all_rows(items, 6, lambda i, r: _all_fields(1)))
        run = _make_run_artifact(items, _all_rows(items, 3, lambda i, r: _all_fields(0)))

        delta = gate.adversarial_composite_delta(baseline, run, CompositeMode.FULL_7)

        assert delta == pytest.approx(-100.0)

    def test_no_adversarial_items_gives_zero(self):
        items = [make_item("nom-0")]
        baseline = _make_baseline(items, _all_rows(items, 6))
        run = _make_run_artifact(items, _all_rows(items, 3))

        delta = gate.adversarial_composite_delta(baseline, run, CompositeMode.FULL_7)

        assert delta == 0.0


# --------------------------------------------------------------------------
# judge_error_rate.
# --------------------------------------------------------------------------


class TestJudgeErrorRate:
    def test_zero_errors_gives_zero_rate(self):
        run = _make_run_artifact([make_item("nom-0")], _all_rows([make_item("nom-0")], 3))

        assert gate.judge_error_rate(run) == 0.0

    def test_rate_counts_only_attempted_calls(self):
        rows = [
            _row("nom-0", 0, {**_all_fields(1), "issue_summary": None}),
            _row("nom-0", 1, _all_fields(1)),
            _row("nom-0", 2, _all_fields(1)),
        ]
        run = _make_run_artifact([make_item("nom-0")], rows)

        # 3 rows x 2 judged fields = 6 attempted calls, 1 errored -> 1/6.
        assert gate.judge_error_rate(run) == pytest.approx(1.0 / 6.0)

    def test_candidate_failure_rows_are_excluded_from_the_denominator(self):
        failure_row = _row("nom-0", 0, _all_fields(0), candidate_failed=True)
        rows = [failure_row, _row("nom-0", 1, _all_fields(1))]
        run = _make_run_artifact([make_item("nom-0")], rows)

        # Only replicate 1's 2 calls are attempted calls; both pass.
        assert gate.judge_error_rate(run) == 0.0


# --------------------------------------------------------------------------
# evaluate_gate: fingerprint check, judge-error budget, composite-mode
# resolution, --seed-regression's fingerprint-check skip + DEMO MODE banner.
# --------------------------------------------------------------------------


class TestEvaluateGatePassFail:
    def test_both_candidates_unchanged_passes(self):
        items = [make_item("nom-0"), make_item("nom-1"), make_item("adv-0", slice_="adversarial")]
        baseline = _make_baseline(items, _all_rows(items, 6))
        run = _make_run_artifact(items, _all_rows(items, 3))

        outcome = gate.evaluate_gate(
            CONFIG,
            baseline_a=baseline,
            baseline_b=baseline,
            run_a=run,
            run_b=run,
            certificate=_certificate("adequate"),
        )

        assert outcome.exit_code == 0
        assert outcome.summary.overall_verdict == "pass"
        assert "Overall verdict: PASS" in outcome.rendered

    def test_one_regressing_candidate_fails_the_whole_gate(self):
        items = [make_item(f"nom-{i}") for i in range(6)]
        items.append(make_item("adv-0", slice_="adversarial"))
        baseline = _make_baseline(items, _all_rows(items, 6))
        good_run = _make_run_artifact(items, _all_rows(items, 3), label="a")
        bad_run = _make_run_artifact(
            items, _all_rows(items, 3, lambda i, r: _all_fields(0)), label="b"
        )
        baseline_b = _make_baseline(items, _all_rows(items, 6), label="b")

        outcome = gate.evaluate_gate(
            CONFIG,
            baseline_a=baseline,
            baseline_b=baseline_b,
            run_a=good_run,
            run_b=bad_run,
            certificate=_certificate("adequate"),
        )

        assert outcome.exit_code == 1
        assert outcome.summary.overall_verdict == "fail"
        assert outcome.summary.candidates[0].verdict == "pass"
        assert outcome.summary.candidates[1].verdict == "fail"


class TestEvaluateGateFingerprintMismatch:
    def test_judge_version_drift_raises_fingerprint_mismatch(self):
        items = [make_item("nom-0"), make_item("adv-0", slice_="adversarial")]
        baseline = _make_baseline(items, _all_rows(items, 6), judge_version="old-judge-hash")
        run = _make_run_artifact(items, _all_rows(items, 3), judge_version="new-judge-hash")

        with pytest.raises(gate.FingerprintMismatchError) as exc_info:
            gate.evaluate_gate(
                CONFIG,
                baseline_a=baseline,
                baseline_b=baseline,
                run_a=run,
                run_b=run,
                certificate=_certificate("adequate"),
            )

        message = str(exc_info.value)
        assert "judge_version" in message
        assert "update-baseline" in message or "re-baseline" in message.lower()


class TestEvaluateGateJudgeErrorBudget:
    def test_over_five_percent_judge_error_rate_raises(self):
        items = [make_item("nom-0"), make_item("adv-0", slice_="adversarial")]
        baseline = _make_baseline(items, _all_rows(items, 6))
        rows = [_row("nom-0", 0, {**_all_fields(1), "issue_summary": None})]
        rows += [_row("nom-0", r, _all_fields(1)) for r in range(1, 3)]
        rows += [_row("adv-0", r, _all_fields(1)) for r in range(3)]
        run = _make_run_artifact(items, rows)

        with pytest.raises(gate.JudgeErrorBudgetExceededError) as exc_info:
            gate.evaluate_gate(
                CONFIG,
                baseline_a=baseline,
                baseline_b=baseline,
                run_a=run,
                run_b=run,
                certificate=_certificate("adequate"),
            )

        assert "a" == exc_info.value.label


class TestEvaluateGateInadequateCertificate:
    def test_inadequate_certificate_uses_deterministic_5_and_flags(self):
        items = [make_item("nom-0"), make_item("adv-0", slice_="adversarial")]
        baseline = _make_baseline(
            items,
            _all_rows(items, 6),
            composite_mode=CompositeMode.DETERMINISTIC_5,
            calibration_verdict="inadequate",
        )
        # Note: a missing judged field would ALSO trip the mode-independent
        # judge-error-rate budget check (judge_error_rate counts attempted
        # judge calls regardless of composite mode -- a judge health signal,
        # not a scoring-mode-dependent one) -- so this fixture keeps every
        # field determinate and instead relies on
        # TestNominalPairedDeltas.test_deterministic_5_mode_never_excludes_for_judge_errors
        # to cover the DETERMINISTIC_5-never-excludes claim directly.
        run = _make_run_artifact(items, _all_rows(items, 3))

        outcome = gate.evaluate_gate(
            CONFIG,
            baseline_a=baseline,
            baseline_b=baseline,
            run_a=run,
            run_b=run,
            certificate=_certificate("inadequate"),
        )

        assert outcome.summary.composite_mode == CompositeMode.DETERMINISTIC_5
        assert outcome.summary.candidates[0].judge_error_excluded == 0
        assert "DETERMINISTIC_5" in outcome.rendered
        assert "Judged fields excluded" in outcome.rendered

    def test_mismatched_composite_mode_in_baseline_is_a_fingerprint_mismatch(self):
        """An inadequate certificate resolves to DETERMINISTIC_5; a baseline
        still recorded under FULL_7 must be treated as a fingerprint
        mismatch (ticket: 'baseline must have matching composite-mode in its
        fingerprint or -> exit 2'), with zero extra code beyond the existing
        composite_mode field comparison in check_fingerprint."""

        items = [make_item("nom-0"), make_item("adv-0", slice_="adversarial")]
        baseline = _make_baseline(
            items,
            _all_rows(items, 6),
            composite_mode=CompositeMode.FULL_7,
            calibration_verdict="inadequate",
        )
        run = _make_run_artifact(items, _all_rows(items, 3))

        with pytest.raises(gate.FingerprintMismatchError) as exc_info:
            gate.evaluate_gate(
                CONFIG,
                baseline_a=baseline,
                baseline_b=baseline,
                run_a=run,
                run_b=run,
                certificate=_certificate("inadequate"),
            )

        assert "composite_mode" in str(exc_info.value)


class TestEvaluateGateSeedRegression:
    def test_seed_regression_skips_fingerprint_check_and_banners_demo_mode(self):
        items = [make_item("nom-0"), make_item("adv-0", slice_="adversarial")]
        baseline = _make_baseline(items, _all_rows(items, 6), prompt_version=1)
        run = _make_run_artifact(
            items, _all_rows(items, 3), prompt_version=DEGRADED_DEMO_PROMPT.version
        )

        # Sanity: without seed_regression, this same mismatched setup raises.
        with pytest.raises(gate.FingerprintMismatchError):
            gate.evaluate_gate(
                CONFIG,
                baseline_a=baseline,
                baseline_b=baseline,
                run_a=run,
                run_b=run,
                certificate=_certificate("adequate"),
                seed_regression=False,
            )

        outcome = gate.evaluate_gate(
            CONFIG,
            baseline_a=baseline,
            baseline_b=baseline,
            run_a=run,
            run_b=run,
            certificate=_certificate("adequate"),
            seed_regression=True,
        )

        assert "DEMO MODE" in outcome.rendered


class TestEvaluateGateRenderedSummaryContent:
    """Ties several ticket acceptance criteria directly to
    ``render_gate_summary``'s actual output on a realistic combined fixture,
    rather than relying solely on T10's own renderer tests: sparse-delta
    warning text at m=3 (rejection impossible), the judge-error exclusion
    count, the adversarial delta always printed with "coarse" guardrail
    wording, MDE, the family false-alarm line, and the gate-design.md link.
    """

    def test_combined_fixture_renders_all_required_elements(self):
        # 6 nominal items: 2 excluded by a run-side judge error, 3 fully
        # regress (delta -100 each -> m=3 nonzero, sparse-delta/"impossible"
        # territory), 1 unchanged. Plus 1 adversarial item, small delta,
        # guardrail not tripped.
        items = [make_item(f"nom-{i}") for i in range(6)]
        items.append(make_item("adv-0", slice_="adversarial"))
        baseline = _make_baseline(items, _all_rows(items, 6))

        def scores(item_id: str, replicate: int) -> dict[str, int | None]:
            # Only replicate 0 errors (item-level exclusion needs just one
            # missing replicate, module docstring) -- keeps the overall
            # judge-error rate under the 5% budget (2 errors / 42 calls =
            # 4.8%) while still excluding both items entirely.
            if item_id in ("nom-0", "nom-1") and replicate == 0:
                return {**_all_fields(1), "issue_summary": None}
            if item_id in ("nom-2", "nom-3", "nom-4"):
                return _all_fields(0)
            return _all_fields(1)

        run = _make_run_artifact(items, _all_rows(items, 3, scores))

        outcome = gate.evaluate_gate(
            CONFIG, baseline_a=baseline, baseline_b=baseline, run_a=run, run_b=run,
            certificate=_certificate("adequate"),
        )

        candidate_a = outcome.summary.candidates[0]
        assert candidate_a.judge_error_excluded == 2
        assert candidate_a.m_nonzero == 3
        assert candidate_a.verdict == "pass"  # m<=4: no rejection possible regardless of size

        rendered = outcome.rendered
        assert "no rejection is possible" in rendered
        assert "2 item(s) excluded from paired deltas" in rendered
        assert "coarse guardrail" in rendered
        assert "not tripped" in rendered
        assert "MDE" in rendered
        assert "Family false-alarm rate" in rendered
        assert "docs/gate-design.md" in rendered


# --------------------------------------------------------------------------
# CLI surface: typer.testing.CliRunner end-to-end.
# --------------------------------------------------------------------------


class _FakeTraceContext:
    """Duck-typed stand-in for ``TraceContext`` -- always "traced", never
    touches Langfuse (mirrors ``test_cli.py``'s own fake)."""

    untraced = False

    @staticmethod
    def for_run(config: object, reportable: bool, **kwargs: object) -> _FakeTraceContext:
        return _FakeTraceContext()

    def candidate_span(self, **kwargs: object):
        import contextlib

        return contextlib.nullcontext()

    def judge_span(self, **kwargs: object):
        import contextlib

        return contextlib.nullcontext()

    def record_item_scores(self, **kwargs: object) -> None:
        pass

    def flush(self) -> None:
        pass


def _fake_build_model_key_factory(
    registry: dict[str, list[FakeModelClient]],
) -> Callable[[str, object], ModelKey]:
    def factory(label: str, config: object) -> ModelKey:
        candidate = FakeModelClient(make_result=lambda *a: success_result())
        judge = FakeModelClient(make_result=lambda *a: judge_pass_result())
        registry.setdefault("candidate", []).append(candidate)
        registry.setdefault("judge", []).append(judge)
        return ModelKey(label=label, candidate_client=candidate, judge_client=judge)

    return factory


def _regressing_factory(regress_label: str) -> Callable[[str, object], ModelKey]:
    def factory(label: str, config: object) -> ModelKey:
        if label == regress_label:
            candidate = FakeModelClient(make_result=lambda *a: wrong_result())
        else:
            candidate = FakeModelClient(make_result=lambda *a: success_result())
        judge = FakeModelClient(make_result=lambda *a: judge_pass_result())
        return ModelKey(label=label, candidate_client=candidate, judge_client=judge)

    return factory


def _factory_with_judge_errors(
    target_label: str, error_every_n: int
) -> Callable[[str, object], ModelKey]:
    def make_judge_result(idx: int, prompt: str, schema: type) -> StructuredResult:
        if idx % error_every_n == 0:
            return judge_error_result()
        return judge_pass_result()

    def factory(label: str, config: object) -> ModelKey:
        candidate = FakeModelClient(make_result=lambda *a: success_result())
        if label == target_label:
            judge = FakeModelClient(make_result=make_judge_result)
        else:
            judge = FakeModelClient(make_result=lambda *a: judge_pass_result())
        return ModelKey(label=label, candidate_client=candidate, judge_client=judge)

    return factory


def _alternating_candidate_for_item(target_item_id: str) -> FakeModelClient:
    """A candidate fake that alternates correct/wrong output specifically
    for ``target_item_id`` (detected via the unique 'From:' address
    ``make_item`` embeds in the rendered prompt) -- drives a large
    run-to-run adversarial composite variance for the guardrail-refusal
    test, mirroring test_baseline.py's alternating-scores technique but
    through the real candidate/judge call path."""

    state = {"n": 0}
    lock = threading.Lock()

    def make_result(idx: int, prompt: str, schema: type) -> StructuredResult:
        if target_item_id not in prompt:
            return success_result()
        with lock:
            n = state["n"]
            state["n"] += 1
        return success_result() if n % 2 == 0 else wrong_result()

    return FakeModelClient(make_result=make_result)


def _write_golden_dataset(items: list[GoldenItem]) -> Path:
    path = Path("data") / "golden" / "golden.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(item.model_dump(mode="json")) for item in items]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_gate_config(
    *, dataset_path: Path, dataset_version: int = 1, k: int = 3, k_baseline: int = 6
) -> Path:
    base = load_config(DEFAULT_CONFIG_PATH)
    updated = base.model_copy(
        update={
            "dataset": base.dataset.model_copy(
                update={"path": str(dataset_path), "version": dataset_version}
            ),
            "k": k,
            "k_baseline": k_baseline,
        }
    )
    path = Path("config.yaml")
    path.write_text(yaml.safe_dump(updated.model_dump(mode="json")), encoding="utf-8")
    return path


def _write_certificate_file(verdict: str = "adequate") -> None:
    cert_dir = Path("data") / "calibration"
    cert_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "judge_version": "judge-version-hash",
        "overall_kappa": 0.7,
        "kappa_ci": [0.5, 0.85],
        "per_candidate_kappa": {"a": 0.7, "b": 0.7},
        "verdict": verdict,
        "ceiling_kappa": None,
        "label_file_hash": "deadbeef",
        "date": "2026-06-01",
    }
    (cert_dir / "certificate.json").write_text(json.dumps(payload), encoding="utf-8")


def _generate_matching_baselines(
    cfg: Config,
    items: list[GoldenItem],
    *,
    calibration_verdict: str = "adequate",
    mode: CompositeMode = CompositeMode.FULL_7,
) -> None:
    for label in ("a", "b"):
        candidate = FakeModelClient(make_result=lambda *a: success_result())
        judge = FakeModelClient(make_result=lambda *a: judge_pass_result())
        generate_baseline(
            cfg,
            ModelKey(label=label, candidate_client=candidate, judge_client=judge),
            dataset=items,
            prompt=EXTRACTION_PROMPT,
            composite_mode=mode,
            calibration_verdict=calibration_verdict,
            runs_root=Path("results/runs"),
            baselines_root=Path("baselines"),
        )


class TestGateCliPassAndFail:
    def test_unchanged_baseline_passes_exit_0(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        items = [
            make_item("nom-0"),
            make_item("nom-1"),
            make_item("adv-0", slice_="adversarial"),
        ]
        dataset_path = _write_golden_dataset(items)
        config_path = _write_gate_config(dataset_path=dataset_path)
        _write_certificate_file("adequate")
        cfg = load_config(config_path)
        _generate_matching_baselines(cfg, items)

        monkeypatch.setattr(cli, "TraceContext", _FakeTraceContext)
        registry: dict[str, list[FakeModelClient]] = {}
        monkeypatch.setattr(cli, "_build_model_key", _fake_build_model_key_factory(registry))

        result = runner.invoke(app, ["gate", "--config", str(config_path)])

        assert result.exit_code == 0, result.output
        assert "Overall verdict: PASS" in result.output

    def test_only_candidate_b_regressing_fails_the_gate(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        items = [make_item(f"nom-{i}") for i in range(6)]
        items.append(make_item("adv-0", slice_="adversarial"))
        dataset_path = _write_golden_dataset(items)
        config_path = _write_gate_config(dataset_path=dataset_path)
        _write_certificate_file("adequate")
        cfg = load_config(config_path)
        _generate_matching_baselines(cfg, items)

        monkeypatch.setattr(cli, "TraceContext", _FakeTraceContext)
        monkeypatch.setattr(cli, "_build_model_key", _regressing_factory("b"))

        result = runner.invoke(app, ["gate", "--config", str(config_path)])

        assert result.exit_code == 1, result.output
        assert "Overall verdict: FAIL" in result.output


class TestGateCliMissingBaseline:
    def test_missing_baseline_exits_2_with_instructions(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        items = [make_item("nom-0"), make_item("adv-0", slice_="adversarial")]
        dataset_path = _write_golden_dataset(items)
        config_path = _write_gate_config(dataset_path=dataset_path)
        _write_certificate_file("adequate")

        monkeypatch.setattr(cli, "TraceContext", _FakeTraceContext)
        registry: dict[str, list[FakeModelClient]] = {}
        monkeypatch.setattr(cli, "_build_model_key", _fake_build_model_key_factory(registry))

        result = runner.invoke(app, ["gate", "--config", str(config_path)])

        assert result.exit_code == 2, result.output
        assert "Traceback" not in result.output
        assert "--update-baseline" in result.output
        assert "compare-vs-old-baseline" in result.output


class TestGateCliFingerprintMismatch:
    def test_tampered_judge_version_exits_2_with_rebaseline_instruction(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        items = [make_item("nom-0"), make_item("adv-0", slice_="adversarial")]
        dataset_path = _write_golden_dataset(items)
        config_path = _write_gate_config(dataset_path=dataset_path)
        _write_certificate_file("adequate")
        cfg = load_config(config_path)
        _generate_matching_baselines(cfg, items)

        baseline_path = Path("baselines") / "a.json"
        baseline = load_baseline(baseline_path)
        tampered_components = replace(
            baseline.fingerprint_components, judge_version="tampered-hash"
        )
        tampered = replace(baseline, fingerprint_components=tampered_components)
        _write_baseline(tampered, baseline_path)

        monkeypatch.setattr(cli, "TraceContext", _FakeTraceContext)
        registry: dict[str, list[FakeModelClient]] = {}
        monkeypatch.setattr(cli, "_build_model_key", _fake_build_model_key_factory(registry))

        result = runner.invoke(app, ["gate", "--config", str(config_path)])

        assert result.exit_code == 2, result.output
        assert "judge_version" in result.output
        assert "re-baseline" in result.output.lower() or "update-baseline" in result.output


class TestGateCliJudgeErrorBudget:
    def test_six_percent_judge_error_rate_exits_2(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        items = [make_item(f"nom-{i}") for i in range(8)]
        items.append(make_item("adv-0", slice_="adversarial"))
        items.append(make_item("adv-1", slice_="adversarial"))
        dataset_path = _write_golden_dataset(items)
        config_path = _write_gate_config(dataset_path=dataset_path)
        _write_certificate_file("adequate")
        cfg = load_config(config_path)
        _generate_matching_baselines(cfg, items)

        monkeypatch.setattr(cli, "TraceContext", _FakeTraceContext)
        # 10 items x k=3 x 2 judged fields = 60 calls; error every 15th -> 4/60 = 6.67% > 5%.
        monkeypatch.setattr(cli, "_build_model_key", _factory_with_judge_errors("a", 15))

        result = runner.invoke(app, ["gate", "--config", str(config_path)])

        assert result.exit_code == 2, result.output
        assert "judge-error" in result.output.lower() or "judge error" in result.output.lower()


class TestGateCliInadequateCertificate:
    def test_inadequate_certificate_uses_deterministic_5_and_flags(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        items = [make_item("nom-0"), make_item("adv-0", slice_="adversarial")]
        dataset_path = _write_golden_dataset(items)
        config_path = _write_gate_config(dataset_path=dataset_path)
        _write_certificate_file("inadequate")
        cfg = load_config(config_path)
        _generate_matching_baselines(
            cfg, items, calibration_verdict="inadequate", mode=CompositeMode.DETERMINISTIC_5
        )

        monkeypatch.setattr(cli, "TraceContext", _FakeTraceContext)
        registry: dict[str, list[FakeModelClient]] = {}
        monkeypatch.setattr(cli, "_build_model_key", _fake_build_model_key_factory(registry))

        result = runner.invoke(app, ["gate", "--config", str(config_path)])

        assert result.exit_code == 0, result.output
        assert "DETERMINISTIC_5" in result.output
        assert "Judged fields excluded" in result.output


class TestGateCliSeedRegression:
    def test_seed_regression_applies_degraded_prompt_skips_fingerprint_banners_demo(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        items = [make_item("nom-0"), make_item("adv-0", slice_="adversarial")]
        dataset_path = _write_golden_dataset(items)
        config_path = _write_gate_config(dataset_path=dataset_path)
        _write_certificate_file("adequate")
        cfg = load_config(config_path)
        # Baseline recorded against the REAL EXTRACTION_PROMPT (prompt_version=1).
        _generate_matching_baselines(cfg, items)

        monkeypatch.setattr(cli, "TraceContext", _FakeTraceContext)
        registry: dict[str, list[FakeModelClient]] = {}
        monkeypatch.setattr(cli, "_build_model_key", _fake_build_model_key_factory(registry))

        result = runner.invoke(app, ["gate", "--config", str(config_path), "--seed-regression"])

        assert result.exit_code != 2, result.output  # fingerprint mismatch must not surface
        assert "DEMO MODE" in result.output

        # Two "a-*" run dirs exist: one from baseline generation (k_baseline,
        # EXTRACTION_PROMPT) and one from this gate invocation (k,
        # DEGRADED_DEMO_PROMPT) -- distinct hashes since prompt_version/k
        # differ. Find the demo one specifically by its recorded prompt_version.
        run_dirs = list((Path("results") / "runs").glob("a-*"))
        artifacts = [load_run(RunDir(path=p)) for p in run_dirs]
        demo_artifacts = [a for a in artifacts if a.prompt_version == DEGRADED_DEMO_PROMPT.version]
        assert len(demo_artifacts) == 1


class TestGateCliTracingFailFast:
    def test_missing_langfuse_keys_fails_before_any_api_call(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
        items = [make_item("nom-0"), make_item("adv-0", slice_="adversarial")]
        dataset_path = _write_golden_dataset(items)
        config_path = _write_gate_config(dataset_path=dataset_path)
        _write_certificate_file("adequate")

        def _forbid_factory(*args: object, **kwargs: object) -> ModelKey:
            raise AssertionError("_build_model_key must not be called before tracing succeeds")

        monkeypatch.setattr(cli, "_build_model_key", _forbid_factory)

        # Gate runs are always reportable=True: TraceContext.for_run raises
        # MissingTracingError directly (never the keyless-warn-and-proceed
        # path, which is reportable=False only -- see tracing.py).
        result = runner.invoke(app, ["gate", "--config", str(config_path)])

        assert result.exit_code != 0
        assert "Traceback" not in result.output
        assert "credentials" in result.output.lower() or "langfuse" in result.output.lower()

    def test_update_baseline_also_fails_fast_without_tracing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
        items = [make_item("nom-0"), make_item("adv-0", slice_="adversarial")]
        dataset_path = _write_golden_dataset(items)
        config_path = _write_gate_config(dataset_path=dataset_path)
        _write_certificate_file("adequate")

        def _forbid_factory(*args: object, **kwargs: object) -> ModelKey:
            raise AssertionError("_build_model_key must not be called before tracing succeeds")

        monkeypatch.setattr(cli, "_build_model_key", _forbid_factory)

        result = runner.invoke(app, ["gate", "--config", str(config_path), "--update-baseline"])

        assert result.exit_code != 0
        assert "Traceback" not in result.output


class TestGateCliUpdateBaseline:
    def test_writes_both_candidates_with_certificate_values(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        items = [make_item("nom-0"), make_item("adv-0", slice_="adversarial")]
        dataset_path = _write_golden_dataset(items)
        config_path = _write_gate_config(dataset_path=dataset_path)
        _write_certificate_file("adequate")

        monkeypatch.setattr(cli, "TraceContext", _FakeTraceContext)
        registry: dict[str, list[FakeModelClient]] = {}
        monkeypatch.setattr(cli, "_build_model_key", _fake_build_model_key_factory(registry))

        result = runner.invoke(app, ["gate", "--config", str(config_path), "--update-baseline"])

        assert result.exit_code == 0, result.output
        baseline_a = load_baseline(Path("baselines") / "a.json")
        baseline_b = load_baseline(Path("baselines") / "b.json")
        assert baseline_a.fingerprint_components.calibration_verdict == "adequate"
        assert baseline_a.fingerprint_components.composite_mode == "FULL_7"
        assert baseline_b.fingerprint_components.calibration_verdict == "adequate"
        assert baseline_a.k_baseline == load_config(config_path).k_baseline

    def test_without_certificate_warns_and_uses_uncalibrated_placeholder(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        items = [make_item("nom-0"), make_item("adv-0", slice_="adversarial")]
        dataset_path = _write_golden_dataset(items)
        config_path = _write_gate_config(dataset_path=dataset_path)
        # Deliberately no certificate file.

        monkeypatch.setattr(cli, "TraceContext", _FakeTraceContext)
        registry: dict[str, list[FakeModelClient]] = {}
        monkeypatch.setattr(cli, "_build_model_key", _fake_build_model_key_factory(registry))

        with pytest.warns(UserWarning, match="uncalibrated"):
            result = runner.invoke(app, ["gate", "--config", str(config_path), "--update-baseline"])

        assert result.exit_code == 0, result.output
        baseline_a = load_baseline(Path("baselines") / "a.json")
        assert baseline_a.fingerprint_components.calibration_verdict == "uncalibrated"
        assert baseline_a.fingerprint_components.composite_mode == "FULL_7"

    def test_refuses_to_write_when_guardrail_floor_fails(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        items = [make_item("nom-0"), make_item("adv-0", slice_="adversarial")]
        dataset_path = _write_golden_dataset(items)
        config_path = _write_gate_config(dataset_path=dataset_path, k_baseline=6)
        _write_certificate_file("adequate")

        monkeypatch.setattr(cli, "TraceContext", _FakeTraceContext)

        def factory(label: str, config: object) -> ModelKey:
            candidate = _alternating_candidate_for_item("adv-0")
            judge = FakeModelClient(make_result=lambda *a: judge_pass_result())
            return ModelKey(label=label, candidate_client=candidate, judge_client=judge)

        monkeypatch.setattr(cli, "_build_model_key", factory)

        result = runner.invoke(app, ["gate", "--config", str(config_path), "--update-baseline"])

        assert result.exit_code != 0
        assert "Traceback" not in result.output
        assert "guardrail" in result.output.lower()
        assert not (Path("baselines") / "a.json").exists()

    def test_mutually_exclusive_flags_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        items = [make_item("nom-0"), make_item("adv-0", slice_="adversarial")]
        dataset_path = _write_golden_dataset(items)
        config_path = _write_gate_config(dataset_path=dataset_path)

        result = runner.invoke(
            app, ["gate", "--config", str(config_path), "--update-baseline", "--seed-regression"]
        )

        assert result.exit_code != 0
        assert "Traceback" not in result.output
