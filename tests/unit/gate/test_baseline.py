"""Baseline artifact I/O, generation, and fingerprint checking (T15, spec §7).

Fake-client/unit-test only -- zero live API calls (real baselines are
generated in T16 after T12-T14 land). Mirrors ``tests/unit/test_runner.py``'s
fake-client style: ``FakeModelClient`` records calls thread-safely so tests
can assert exact call counts.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import BaseModel

from harness.config import Config, load_config
from harness.gate.baseline import (
    BaselineFile,
    BaselineFormatError,
    FingerprintComponents,
    Mismatch,
    check_fingerprint,
    check_guardrail_floor,
    fingerprint_components_from_run,
    generate_baseline,
    load_baseline,
)
from harness.judge.judge import JudgeVerdict
from harness.models import StructuredResult, Usage
from harness.prompts import EXTRACTION_PROMPT
from harness.runner import ModelKey, RunRow
from harness.schema import EmailInput, GoldenExpected, GoldenItem, GoldenMeta, TicketExtraction
from harness.scoring.composite import CompositeMode

DEFAULT_CONFIG_PATH = Path(__file__).parents[3] / "configs" / "default.yaml"


def _config() -> Config:
    return load_config(DEFAULT_CONFIG_PATH)


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


def judge_pass_result() -> StructuredResult:
    output = JudgeVerdict(verdict="pass", rationale="Same issue and action.")
    return StructuredResult(
        output=output,
        failure=None,
        raw=output.model_dump_json(),
        usage=Usage(input_tokens=5, output_tokens=5),
        served_model_version="judge-v1",
    )


@dataclass
class FakeModelClient:
    """Test double for the ``ModelClient`` protocol -- thread-safe call
    recording, mirrors ``test_runner.py``'s fake."""

    make_result: Callable[[int, str, type[BaseModel]], StructuredResult]
    calls: list[tuple[str, type]] = field(default_factory=list)

    def complete_structured(self, prompt: str, schema: type[BaseModel]) -> StructuredResult:
        idx = len(self.calls)
        self.calls.append((prompt, schema))
        return self.make_result(idx, prompt, schema)

    @property
    def call_count(self) -> int:
        return len(self.calls)


def _model_key(
    candidate: FakeModelClient | None = None, judge: FakeModelClient | None = None
) -> ModelKey:
    return ModelKey(
        label="a",
        candidate_client=candidate or FakeModelClient(make_result=lambda *a: success_result()),
        judge_client=judge or FakeModelClient(make_result=lambda *a: judge_pass_result()),
    )


def _two_item_dataset() -> list[GoldenItem]:
    """One nominal + one adversarial item -- the adversarial one is required
    for the noise-floor measurement (needs >=1 adversarial item to have any
    replicate-level composites to measure a standard error over)."""

    return [make_item("nom-0", slice_="nominal"), make_item("adv-0", slice_="adversarial")]


def _row(item_id: str, replicate: int, field_scores: dict[str, int | None]) -> RunRow:
    judged = {"issue_summary": None, "requested_action": None}
    return RunRow(
        item_id=item_id,
        replicate=replicate,
        raw_output="{}",
        raw_judge=dict(judged),
        field_scores=field_scores,
        usage={"input_tokens": 1, "output_tokens": 1},
        served_model_version="candidate-v1",
        judge_rationales=dict(judged),
        judge_usage=None,
    )


def _all_seven_scores(value: int) -> dict[str, int | None]:
    return {
        "category": value,
        "priority": value,
        "customer_name": value,
        "order_id": value,
        "product_name": value,
        "issue_summary": value,
        "requested_action": value,
    }


def _fingerprint_components(**overrides) -> FingerprintComponents:
    defaults = dict(
        prompt_version=1,
        dataset_version=1,
        served_versions={"candidate_a": "candidate-v1", "judge": "judge-v1"},
        judge_version="judge-version-hash",
        composite_mode="FULL_7",
        calibration_verdict="uncalibrated",
    )
    defaults.update(overrides)
    return FingerprintComponents(**defaults)


def _minimal_baseline(
    *, fingerprint_components=None, adversarial_noise_floor_se=0.0
) -> BaselineFile:
    return BaselineFile(
        schema_version=1,
        label="a",
        k_baseline=6,
        items=(),
        rows=(),
        fingerprint="fingerprint-hash",
        fingerprint_components=fingerprint_components or _fingerprint_components(),
        adversarial_noise_floor_se=adversarial_noise_floor_se,
        created_at="2026-07-04T00:00:00+00:00",
    )


class TestGenerateBaselineUsesKBaseline:
    def test_candidate_call_count_is_items_times_k_baseline(self, tmp_path):
        items = _two_item_dataset()
        candidate = FakeModelClient(make_result=lambda *a: success_result())
        judge = FakeModelClient(make_result=lambda *a: judge_pass_result())
        model_key = _model_key(candidate, judge)
        config = _config()
        assert config.k_baseline == 6

        generate_baseline(
            config,
            model_key,
            dataset=items,
            runs_root=tmp_path / "runs",
            baselines_root=tmp_path / "baselines",
        )

        assert candidate.call_count == len(items) * config.k_baseline
        # 2 judged fields x (items x k_baseline) rows.
        assert judge.call_count == 2 * len(items) * config.k_baseline


class TestGenerateBaselineRoundTrip:
    def test_written_baseline_reloads_with_per_field_scores_and_raw_outputs(self, tmp_path):
        items = _two_item_dataset()
        model_key = _model_key()
        config = _config()

        baseline = generate_baseline(
            config,
            model_key,
            dataset=items,
            runs_root=tmp_path / "runs",
            baselines_root=tmp_path / "baselines",
        )

        baseline_path = tmp_path / "baselines" / "a.json"
        assert baseline_path.exists()
        reloaded = load_baseline(baseline_path)

        assert reloaded.label == "a"
        assert reloaded.k_baseline == 6
        assert reloaded.fingerprint == baseline.fingerprint
        assert len(reloaded.items) == 2
        # items x k_baseline replicates, all persisted.
        assert len(reloaded.rows) == 2 * 6

        row = next(r for r in reloaded.rows if r.item_id == "nom-0" and r.replicate == 0)
        assert len(row.field_scores) == 7
        assert all(v == 1 for v in row.field_scores.values())
        assert row.raw_output != ""
        assert row.raw_judge["issue_summary"] is not None

        # Deterministic fake clients always agree -> zero run-to-run noise,
        # which trivially satisfies the guardrail floor.
        assert reloaded.adversarial_noise_floor_se == 0.0
        assert check_guardrail_floor(reloaded) is True

    def test_reloaded_fingerprint_components_match_generated(self, tmp_path):
        items = _two_item_dataset()
        model_key = _model_key()
        config = _config()

        baseline = generate_baseline(
            config,
            model_key,
            dataset=items,
            runs_root=tmp_path / "runs",
            baselines_root=tmp_path / "baselines",
        )
        reloaded = load_baseline(tmp_path / "baselines" / "a.json")

        assert reloaded.fingerprint_components == baseline.fingerprint_components
        assert (
            reloaded.fingerprint_components.judge_version
            == baseline.fingerprint_components.judge_version
        )
        assert reloaded.fingerprint_components.served_versions["candidate_a"] == "candidate-v1"
        assert reloaded.fingerprint_components.served_versions["judge"] == "judge-v1"


class TestFingerprintComponentsFromRun:
    def test_builds_components_from_run_artifact_plus_real_verdicts(self, tmp_path):
        from harness.runner import load_run, run_eval

        items = _two_item_dataset()
        model_key = _model_key()
        config = _config()

        run_dir = run_eval(
            config, model_key, k=1, dataset=items, prompt=EXTRACTION_PROMPT, runs_root=tmp_path
        )
        artifact = load_run(run_dir)

        components = fingerprint_components_from_run(
            artifact,
            composite_mode=CompositeMode.DETERMINISTIC_5,
            calibration_verdict="adequate",
        )

        assert components.prompt_version == artifact.prompt_version
        assert components.dataset_version == artifact.dataset_version
        assert components.judge_version == artifact.judge_version
        assert components.composite_mode == "DETERMINISTIC_5"
        assert components.calibration_verdict == "adequate"
        assert components.served_versions == artifact.served_versions


class TestCheckFingerprintMatch:
    def test_identical_components_return_empty_list(self):
        baseline = _minimal_baseline()
        run = _fingerprint_components()

        assert check_fingerprint(baseline, run) == []


class TestCheckFingerprintJudgeVersionDrift:
    def test_judge_version_drift_is_exactly_one_named_mismatch(self):
        baseline = _minimal_baseline()
        run = _fingerprint_components(judge_version="a-different-judge-version-hash")

        mismatches = check_fingerprint(baseline, run)

        assert len(mismatches) == 1
        assert mismatches[0] == Mismatch(
            "judge_version", "judge-version-hash", "a-different-judge-version-hash"
        )


class TestCheckFingerprintMultipleComponents:
    def test_multiple_differing_components_are_all_named(self):
        baseline = _minimal_baseline(
            fingerprint_components=_fingerprint_components(prompt_version=1, dataset_version=1)
        )
        run = _fingerprint_components(
            prompt_version=2,
            dataset_version=2,
            composite_mode="DETERMINISTIC_5",
            calibration_verdict="inadequate",
        )

        mismatches = check_fingerprint(baseline, run)
        fields = {m.field for m in mismatches}

        assert fields == {
            "prompt_version",
            "dataset_version",
            "composite_mode",
            "calibration_verdict",
        }
        assert len(mismatches) == 4

    def test_served_versions_key_value_drift_is_named_by_key(self):
        baseline = _minimal_baseline(
            fingerprint_components=_fingerprint_components(
                served_versions={"candidate_a": "candidate-v1", "judge": "judge-v1"}
            )
        )
        run = _fingerprint_components(
            served_versions={"candidate_a": "candidate-v2", "judge": "judge-v1"}
        )

        mismatches = check_fingerprint(baseline, run)

        assert mismatches == [
            Mismatch("served_versions.candidate_a", "candidate-v1", "candidate-v2")
        ]


class TestCheckFingerprintMissingServedVersionKey:
    """A manifest missing an id field entirely must be a mismatch
    (fail-closed) -- never silently treated as equal-by-absence."""

    def test_key_present_in_baseline_but_absent_in_run_is_a_mismatch(self):
        baseline = _minimal_baseline(
            fingerprint_components=_fingerprint_components(
                served_versions={"candidate_a": "candidate-v1", "judge": "judge-v1"}
            )
        )
        run = _fingerprint_components(served_versions={"candidate_a": "candidate-v1"})

        mismatches = check_fingerprint(baseline, run)

        assert mismatches == [Mismatch("served_versions.judge", "judge-v1", None)]

    def test_key_present_in_run_but_absent_in_baseline_is_a_mismatch(self):
        baseline = _minimal_baseline(
            fingerprint_components=_fingerprint_components(
                served_versions={"candidate_a": "candidate-v1"}
            )
        )
        run = _fingerprint_components(
            served_versions={"candidate_a": "candidate-v1", "judge": "judge-v1"}
        )

        mismatches = check_fingerprint(baseline, run)

        assert mismatches == [Mismatch("served_versions.judge", None, "judge-v1")]


class TestLoadBaselineUnrecognizedFormat:
    """Loading a v0/unrecognized-schema baseline file must fail loudly with
    a distinct exception naming the format problem -- never a silent
    misparse or partial load."""

    def test_missing_schema_version_key_fails_loudly(self, tmp_path):
        legacy_v0_payload = {
            "label": "a",
            "k_baseline": 6,
            "items": [],
            "rows": [],
        }
        path = tmp_path / "a.json"
        path.write_text(json.dumps(legacy_v0_payload), encoding="utf-8")

        with pytest.raises(BaselineFormatError) as exc_info:
            load_baseline(path)

        assert "schema_version" in str(exc_info.value)

    def test_unrecognized_schema_version_value_fails_loudly(self, tmp_path):
        payload = {"schema_version": 0, "label": "a"}
        path = tmp_path / "a.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(BaselineFormatError) as exc_info:
            load_baseline(path)

        assert "schema_version" in str(exc_info.value)
        assert exc_info.value.found_version == 0


class TestGuardrailFloor:
    """Fixture with SE > 10/3 points -> check fails; SE <= 10/3 -> passes
    (spec §7's adversarial guardrail, verified at baseline time)."""

    def test_se_above_threshold_over_three_fails(self):
        baseline = _minimal_baseline(adversarial_noise_floor_se=4.0)  # 3*4=12 > 10

        assert check_guardrail_floor(baseline) is False

    def test_se_at_or_below_threshold_over_three_passes(self):
        baseline = _minimal_baseline(adversarial_noise_floor_se=3.0)  # 3*3=9 <= 10

        assert check_guardrail_floor(baseline) is True

    def test_boundary_exactly_one_third_of_threshold_passes(self):
        baseline = _minimal_baseline(adversarial_noise_floor_se=10.0 / 3.0)

        assert check_guardrail_floor(baseline) is True


class TestMeasureAdversarialNoiseFloorArithmetic:
    """Direct arithmetic proof, decoupled from run_eval/fake-client call
    ordering: hand-built rows with known per-replicate adversarial
    composites, checked against a hand-computed standard error."""

    def test_measures_standard_error_of_replicate_level_composites(self):
        from harness.gate.baseline import _measure_adversarial_noise_floor

        items = [make_item("adv-0", slice_="adversarial"), make_item("nom-0", slice_="nominal")]
        # Adversarial item's composite alternates 100 / (6/7*100) across
        # replicates by flipping exactly one of seven fields; the nominal
        # item is always perfect and must not affect the measurement.
        rows = []
        for replicate in range(4):
            adv_scores = _all_seven_scores(1)
            if replicate % 2 == 1:
                adv_scores["category"] = 0
            rows.append(_row("adv-0", replicate, adv_scores))
            rows.append(_row("nom-0", replicate, _all_seven_scores(1)))

        se = _measure_adversarial_noise_floor(items, rows, CompositeMode.FULL_7, k_run=6)

        expected_values = [100.0, 600.0 / 7.0, 100.0, 600.0 / 7.0]
        expected_se = statistics.stdev(expected_values) / math.sqrt(6)
        assert se == pytest.approx(expected_se)
        assert se > 0.0

    def test_excludes_replicates_with_a_missing_judged_field(self):
        from harness.gate.baseline import _measure_adversarial_noise_floor

        items = [make_item("adv-0", slice_="adversarial")]
        rows = [
            _row("adv-0", 0, _all_seven_scores(1)),
            _row("adv-0", 1, {**_all_seven_scores(1), "issue_summary": None}),
            _row("adv-0", 2, _all_seven_scores(1)),
        ]

        # Only replicates 0 and 2 are usable (replicate 1's missing judged
        # field must exclude it, mirroring the gate's own missing != fail
        # exclusion) -- both usable replicates are identical (composite
        # 100), so the standard error over them is exactly zero.
        se = _measure_adversarial_noise_floor(items, rows, CompositeMode.FULL_7, k_run=3)

        assert se == 0.0

    def test_raises_when_fewer_than_two_replicates_are_measurable(self):
        from harness.gate.baseline import _measure_adversarial_noise_floor

        items = [make_item("adv-0", slice_="adversarial")]
        rows = [_row("adv-0", 0, _all_seven_scores(1))]

        with pytest.raises(ValueError):
            _measure_adversarial_noise_floor(items, rows, CompositeMode.FULL_7, k_run=3)

    def test_discriminating_case_k_run_3_fails_guardrail_but_k_baseline_6_would_pass(self):
        """Discriminating case verifying the fix: SE denominator must use k_run (gate run
        replicate count = 3), not len(per_replicate) (baseline k = 6).

        Per-replicate adversarial composites alternate 100 / (6/7*100 ≈ 85.71) across
        6 baseline replicates -> stdev ≈ 50/7 ≈ 7.14.

        - With k_run=3: SE = 7.14/sqrt(3) ≈ 4.12 → 3*SE ≈ 12.36 > 10 → FAILS (correct)
        - Old formula sqrt(6): SE = 7.14/sqrt(6) ≈ 2.92 → 3*SE ≈ 8.75 ≤ 10 → PASSES (wrong)

        This test uses the corrected API and asserts the check FAILS, pinning k_run=3.
        """
        from harness.gate.baseline import _measure_adversarial_noise_floor

        items = [make_item("adv-0", slice_="adversarial")]
        # Create 6 baseline replicates with alternating perfect/one-field-wrong pattern.
        rows = []
        for replicate in range(6):
            adv_scores = _all_seven_scores(1)
            if replicate % 2 == 1:
                adv_scores["category"] = 0
            rows.append(_row("adv-0", replicate, adv_scores))

        # Compute SE with k_run=3 (gate run replicate count).
        se = _measure_adversarial_noise_floor(items, rows, CompositeMode.FULL_7, k_run=3)

        # Hand-compute the expected SE:
        # Replicates have composites: [100, 600/7, 100, 600/7, 100, 600/7]
        expected_values = [100.0, 600.0 / 7.0, 100.0, 600.0 / 7.0, 100.0, 600.0 / 7.0]
        expected_stdev = statistics.stdev(expected_values)
        expected_se = expected_stdev / math.sqrt(3)  # k_run=3, NOT len(per_replicate)=6
        assert se == pytest.approx(expected_se)

        # The guardrail floor check must FAIL (3*SE ≈ 12.36 > 10).
        baseline = _minimal_baseline(adversarial_noise_floor_se=se)
        assert check_guardrail_floor(baseline) is False


class TestGenerateBaselineNoneJudgeErrorRoundTrip:
    """Verify that None judge-error scores (verdict None for a field) survive
    round-trip through JSON write and load (F2)."""

    def test_none_judge_error_scores_round_trip_through_json(self, tmp_path):
        """Generate a baseline with a judge that returns errors (verdict None)
        for at least one field of at least one item, write to JSON, reload,
        and verify the None is preserved exactly (not coerced to 0 or missing)."""

        items = _two_item_dataset()

        # Create a fake judge that returns a judge error (verdict=None) for
        # issue_summary on the first call (replicate 0, item 0), then passes
        # normally for all other calls.
        def judge_with_error(idx: int, prompt: str, schema: type[BaseModel]) -> StructuredResult:
            if idx == 0:  # First judge call -> error
                return StructuredResult(
                    output=None,
                    failure="refusal",  # Judge refusal = error, not fail
                    raw="Judge refused to respond",
                    usage=Usage(input_tokens=5, output_tokens=0),
                    served_model_version="judge-v1",
                )
            else:
                return judge_pass_result()

        model_key = _model_key(
            candidate=FakeModelClient(make_result=lambda *a: success_result()),
            judge=FakeModelClient(make_result=judge_with_error),
        )
        config = _config()

        baseline = generate_baseline(
            config,
            model_key,
            dataset=items,
            runs_root=tmp_path / "runs",
            baselines_root=tmp_path / "baselines",
        )

        # Verify the baseline has the None value in the first row's judged field.
        first_row = baseline.rows[0]
        assert first_row.replicate == 0
        assert first_row.item_id == "nom-0"
        # The first judge call fails, so issue_summary should be None.
        assert first_row.field_scores["issue_summary"] is None
        # Other fields should still have valid scores (0 or 1).
        assert first_row.field_scores["requested_action"] in (0, 1)
        assert first_row.field_scores["category"] in (0, 1)

        # Reload the baseline from disk and verify None is preserved.
        baseline_path = tmp_path / "baselines" / "a.json"
        reloaded = load_baseline(baseline_path)

        reloaded_first_row = reloaded.rows[0]
        assert reloaded_first_row.field_scores["issue_summary"] is None
        # Verify other fields are still valid (not corrupted by JSON round-trip).
        assert reloaded_first_row.field_scores["requested_action"] in (0, 1)
        assert reloaded_first_row.field_scores["category"] in (0, 1)
