"""Synthetic-fixture tests for judge calibration (T14): triple reconstruction,
agreement statistics, adequacy verdicts, self-consistency, the test-retest
ceiling, and the committed certificate. No live API calls anywhere in this
module -- every judge call is served by a hand-written fake client.
"""

from __future__ import annotations

import contextlib
import json
import threading
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel
from typer.testing import CliRunner

import harness.calibrate as calibrate
import harness.cli as cli
import harness.stats.agreement as agreement_module
from harness.calibrate import (
    CalibrationResult,
    JudgedTriple,
    PairedJudgment,
    SelfConsistencyResult,
    Triple,
    build_certificate,
    build_triples,
    check_disjoint_from_golden,
    compute_agreement,
    compute_retest_ceiling,
    decide_verdict,
    hash_label_file,
    judge_triples,
    load_calibration_labels,
    measure_self_consistency,
    pair_with_labels,
    per_candidate_divergence_flag,
    render_calibration_report,
    resolve_certificate_date,
    run_calibration,
    select_fixed_self_consistency_triples,
    write_certificate,
)
from harness.cli import app
from harness.config import load_config
from harness.judge.judge import Judge, JudgeVerdict
from harness.judge.rubric import judge_version
from harness.models import StructuredResult, Usage
from harness.prompts import EXTRACTION_PROMPT
from harness.runner import DEFAULT_RUNS_ROOT, ModelKey, RunArtifact, RunDir, RunRow, run_eval
from harness.schema import (
    CalibrationLabel,
    Certificate,
    EmailInput,
    GoldenExpected,
    GoldenItem,
    GoldenMeta,
    TicketExtraction,
)
from harness.stats.agreement import KappaResult

# --------------------------------------------------------------------------
# Shared fixtures/helpers.
# --------------------------------------------------------------------------


def make_item(
    item_id: str, *, issue_summary: str = "ref issue", requested_action: str = "ref action"
) -> GoldenItem:
    return GoldenItem(
        id=item_id,
        email=EmailInput(
            **{"from": f"{item_id}@example.com", "subject": f"Subject {item_id}", "body": "Body."}
        ),
        expected=GoldenExpected(
            category="billing",
            priority="normal",
            customer_name="Jane Doe",
            order_id=None,
            product_name=None,
            issue_summary=issue_summary,
            requested_action=requested_action,
        ),
        meta=GoldenMeta(
            slice="nominal",
            categories=["billing"],
            difficulty=1,
            generator="gpt-4",
            edited=False,
            notes="",
        ),
    )


def make_row(
    item_id: str,
    replicate: int,
    *,
    issue_summary: str,
    requested_action: str,
    raw_output: str | None = None,
) -> RunRow:
    if raw_output is None:
        raw_output = json.dumps(
            {
                "category": "billing",
                "priority": "normal",
                "customer_name": "Jane Doe",
                "order_id": None,
                "product_name": None,
                "issue_summary": issue_summary,
                "requested_action": requested_action,
            }
        )
    return RunRow(
        item_id=item_id,
        replicate=replicate,
        raw_output=raw_output,
        raw_judge={"issue_summary": "{}", "requested_action": "{}"},
        field_scores={
            "category": 1,
            "priority": 1,
            "customer_name": 1,
            "order_id": 1,
            "product_name": 1,
            "issue_summary": 1,
            "requested_action": 1,
        },
        usage={"input_tokens": 10, "output_tokens": 5},
        served_model_version="candidate-v1",
        judge_rationales={"issue_summary": "ok", "requested_action": "ok"},
        judge_usage={
            "issue_summary": {"input_tokens": 1, "output_tokens": 1},
            "requested_action": {"input_tokens": 1, "output_tokens": 1},
        },
    )


def make_run_artifact(model_key: str, items: list[GoldenItem], rows: list[RunRow]) -> RunArtifact:
    return RunArtifact(
        run_dir=RunDir(path=Path("unused")),
        model_key=model_key,
        k=1,
        prompt_version=1,
        dataset_version=1,
        items=tuple(items),
        rows=tuple(rows),
        served_versions={},
        judge_version="unused-run-judge-version",
        fingerprint="unused-fingerprint",
        completed=True,
        untraced=False,
    )


def make_label(
    item_id: str,
    candidate: str,
    field: str,
    verdict: str,
    *,
    round_: str = "initial",
    label_date_: str = "2026-06-01",
    label_id: str | None = None,
    candidate_value: str = "unused-value",
) -> CalibrationLabel:
    """``candidate_value`` (finding F1) defaults to an arbitrary placeholder
    for tests that never pair this label against a real judged triple (date/
    hash-file/round-trip tests, and retest-round labels, which are only ever
    compared to each other, never to a candidate output). Any test that DOES
    flow through ``pair_with_labels``/``run_calibration`` for its ``"initial"``
    round labels must pass the SAME ``candidate_value`` string the
    corresponding ``Triple``/candidate output actually carries, or the F1
    binding check will (correctly) raise ``CalibrationBindingError``."""

    return CalibrationLabel(
        label_id=label_id or f"lbl-{item_id}-{candidate}-{field}-{round_}",
        item_id=item_id,
        candidate=candidate,
        field=field,
        verdict=verdict,
        critique="scripted",
        label_date=label_date_,
        round=round_,
        output_sha256=calibrate.hash_output(candidate_value),
    )


@dataclass
class _KeyedJudgeClient:
    """Fake ``ModelClient``: the verdict for a call is a function of the
    triple's ``candidate_value`` (parsed back out of the rendered prompt) and
    how many times THIS candidate_value has been seen before -- lets a test
    script both "always answer X for this triple" (ignore the call index) and
    "answer differently on the Nth repeat" (self-consistency flips)."""

    verdict_for: Callable[[str, int], str]
    calls: list[str] = field(default_factory=list)
    _counts: dict[str, int] = field(default_factory=dict)

    def complete_structured(self, prompt: str, schema: type[BaseModel]) -> StructuredResult:
        self.calls.append(prompt)
        candidate_value = prompt.rsplit("Candidate value: ", 1)[1].rstrip("\n")
        idx = self._counts.get(candidate_value, 0)
        self._counts[candidate_value] = idx + 1
        verdict = self.verdict_for(candidate_value, idx)
        if verdict == "__error__":
            return StructuredResult(
                output=None,
                failure="schema_invalid",
                raw="not valid json",
                usage=Usage(input_tokens=1, output_tokens=1),
                served_model_version="judge-test-v1",
            )
        output = JudgeVerdict(verdict=verdict, rationale="scripted")
        return StructuredResult(
            output=output,
            failure=None,
            raw=output.model_dump_json(),
            usage=Usage(input_tokens=1, output_tokens=1),
            served_model_version="judge-test-v1",
        )


def _judge(verdict_for: Callable[[str, int], str]) -> Judge:
    return Judge(_KeyedJudgeClient(verdict_for=verdict_for))


# --------------------------------------------------------------------------
# build_triples
# --------------------------------------------------------------------------


class TestBuildTriples:
    def test_reconstructs_one_triple_per_judged_field(self):
        item = make_item("cal-001", issue_summary="ref-issue", requested_action="ref-action")
        row = make_row("cal-001", 0, issue_summary="cand-issue", requested_action="cand-action")
        artifact = make_run_artifact("a", [item], [row])

        triples = build_triples("a", artifact)

        assert len(triples) == 2
        by_field = {t.field: t for t in triples}
        assert by_field["issue_summary"].reference == "ref-issue"
        assert by_field["issue_summary"].candidate_value == "cand-issue"
        assert by_field["issue_summary"].candidate == "a"
        assert by_field["issue_summary"].item_id == "cal-001"
        assert by_field["issue_summary"].email == item.email
        assert by_field["requested_action"].candidate_value == "cand-action"

    def test_deterministic_item_id_order(self):
        items = [make_item("cal-003"), make_item("cal-001"), make_item("cal-002")]
        rows = [
            make_row(i.id, 0, issue_summary=f"{i.id}-issue", requested_action=f"{i.id}-action")
            for i in items
        ]
        artifact = make_run_artifact("a", items, rows)

        triples = build_triples("a", artifact)

        assert [t.item_id for t in triples] == [
            "cal-001",
            "cal-001",
            "cal-002",
            "cal-002",
            "cal-003",
            "cal-003",
        ]

    def test_skips_item_with_schema_invalid_raw_output(self):
        item = make_item("cal-001")
        row = make_row("cal-001", 0, issue_summary="x", requested_action="y", raw_output="not json")
        artifact = make_run_artifact("a", [item], [row])

        triples = build_triples("a", artifact)

        assert triples == []

    def test_skips_item_with_no_rows(self):
        item = make_item("cal-001")
        artifact = make_run_artifact("a", [item], [])

        assert build_triples("a", artifact) == []

    def test_uses_lowest_replicate_when_multiple_present(self):
        item = make_item("cal-001")
        rows = [
            make_row("cal-001", 1, issue_summary="replicate-1", requested_action="r1"),
            make_row("cal-001", 0, issue_summary="replicate-0", requested_action="r0"),
        ]
        artifact = make_run_artifact("a", [item], rows)

        triples = build_triples("a", artifact)

        assert all(t.candidate_value in ("replicate-0", "r0") for t in triples)


# --------------------------------------------------------------------------
# judge_triples
# --------------------------------------------------------------------------


class TestJudgeTriples:
    def test_judges_every_triple_once_in_order(self):
        triples = [
            Triple("cal-001", "a", "issue_summary", make_item("cal-001").email, "ref", "val-1"),
            Triple("cal-001", "a", "requested_action", make_item("cal-001").email, "ref", "val-2"),
        ]
        judge = _judge(lambda cv, idx: "pass" if cv == "val-1" else "fail")

        judged = judge_triples(judge, triples)

        assert [j.verdict for j in judged] == ["pass", "fail"]
        assert all(j.error is None for j in judged)

    def test_judge_error_surfaces_as_none_verdict_not_fail(self):
        email = make_item("cal-001").email
        triples = [Triple("cal-001", "a", "issue_summary", email, "ref", "val")]
        judge = _judge(lambda cv, idx: "__error__")

        judged = judge_triples(judge, triples)

        assert judged[0].verdict is None
        assert judged[0].verdict != "fail"
        assert judged[0].error is not None


# --------------------------------------------------------------------------
# pair_with_labels
# --------------------------------------------------------------------------


class TestPairWithLabels:
    def test_pairs_matching_labels_and_excludes_judge_errors_and_unlabeled(self):
        email = make_item("cal-001").email
        triples = [
            Triple("cal-001", "a", "issue_summary", email, "ref", "v1"),  # labeled, pass/pass
            Triple("cal-001", "a", "requested_action", email, "ref", "v2"),  # judge error
            Triple("cal-002", "a", "issue_summary", email, "ref", "v3"),  # unlabeled
        ]
        judged = [
            JudgedTriple(triples[0], verdict="pass", error=None, rationale="ok"),
            JudgedTriple(triples[1], verdict=None, error="refusal", rationale=None),
            JudgedTriple(triples[2], verdict="pass", error=None, rationale="ok"),
        ]
        labels = [
            make_label("cal-001", "a", "issue_summary", "pass", candidate_value="v1"),
            make_label("cal-001", "a", "requested_action", "pass", candidate_value="v2"),
        ]

        paired, judge_errors, unlabeled = pair_with_labels(judged, labels, round_="initial")

        assert len(paired) == 1
        assert paired[0].item_id == "cal-001"
        assert paired[0].owner_verdict == "pass"
        assert paired[0].judge_verdict == "pass"
        assert judge_errors == 1
        assert unlabeled == 1

    def test_duplicate_label_for_same_key_raises(self):
        labels = [
            make_label("cal-001", "a", "issue_summary", "pass", label_id="lbl-1"),
            make_label("cal-001", "a", "issue_summary", "fail", label_id="lbl-2"),
        ]

        with pytest.raises(ValueError, match="duplicate"):
            pair_with_labels([], labels, round_="initial")

    def test_round_selection_ignores_other_round(self):
        email = make_item("cal-001").email
        triple = Triple("cal-001", "a", "issue_summary", email, "ref", "v1")
        judged = [JudgedTriple(triple, verdict="pass", error=None, rationale="ok")]
        labels = [make_label("cal-001", "a", "issue_summary", "fail", round_="retest")]

        paired, _, unlabeled = pair_with_labels(judged, labels, round_="initial")

        assert paired == []
        assert unlabeled == 1


# --------------------------------------------------------------------------
# pair_with_labels: output-binding check (finding F1)
# --------------------------------------------------------------------------


class TestPairWithLabelsOutputBinding:
    def test_hash_mismatch_raises_calibration_binding_error_naming_the_key(self):
        email = make_item("cal-001").email
        triple = Triple("cal-001", "a", "issue_summary", email, "ref", "v1")
        judged = [JudgedTriple(triple, verdict="pass", error=None, rationale="ok")]
        # Label was written against a DIFFERENT candidate_value than the one
        # actually judged now -- e.g. the run directory was regenerated.
        labels = [make_label("cal-001", "a", "issue_summary", "pass", candidate_value="stale-v1")]

        with pytest.raises(calibrate.CalibrationBindingError) as excinfo:
            pair_with_labels(judged, labels, round_="initial")

        assert excinfo.value.mismatches == (("cal-001", "a", "issue_summary"),)
        assert "cal-001" in str(excinfo.value)

    def test_one_mismatch_blocks_all_pairing_not_just_the_bad_key(self):
        """All-or-nothing (F1): a single mismatched label must prevent EVERY
        pair from being returned, including otherwise-correctly-bound ones."""

        email = make_item("cal-001").email
        good_triple = Triple("cal-001", "a", "issue_summary", email, "ref", "good-value")
        bad_triple = Triple("cal-002", "a", "issue_summary", email, "ref", "good-value-2")
        judged = [
            JudgedTriple(good_triple, verdict="pass", error=None, rationale="ok"),
            JudgedTriple(bad_triple, verdict="pass", error=None, rationale="ok"),
        ]
        labels = [
            make_label("cal-001", "a", "issue_summary", "pass", candidate_value="good-value"),
            make_label(
                "cal-002", "a", "issue_summary", "pass", candidate_value="wrong-recorded-value"
            ),
        ]

        with pytest.raises(calibrate.CalibrationBindingError) as excinfo:
            pair_with_labels(judged, labels, round_="initial")

        assert excinfo.value.mismatches == (("cal-002", "a", "issue_summary"),)

    def test_matching_hash_pairs_normally(self):
        email = make_item("cal-001").email
        triple = Triple("cal-001", "a", "issue_summary", email, "ref", "exact-value")
        judged = [JudgedTriple(triple, verdict="pass", error=None, rationale="ok")]
        labels = [
            make_label("cal-001", "a", "issue_summary", "pass", candidate_value="exact-value")
        ]

        paired, judge_errors, unlabeled = pair_with_labels(judged, labels, round_="initial")

        assert len(paired) == 1
        assert judge_errors == 0
        assert unlabeled == 0

    def test_unlabeled_triple_is_never_hash_checked(self):
        """A triple with no matching label at all can't mismatch -- there is
        no label to compare against, so it is simply counted unlabeled."""

        email = make_item("cal-001").email
        triple = Triple("cal-001", "a", "issue_summary", email, "ref", "anything")
        judged = [JudgedTriple(triple, verdict="pass", error=None, rationale="ok")]

        paired, judge_errors, unlabeled = pair_with_labels(judged, [], round_="initial")

        assert paired == []
        assert judge_errors == 0
        assert unlabeled == 1


# --------------------------------------------------------------------------
# hash_output
# --------------------------------------------------------------------------


class TestHashOutput:
    def test_matches_manual_sha256_utf8_no_trimming(self):
        import hashlib

        value = "  candidate value with spaces  \n"

        assert calibrate.hash_output(value) == hashlib.sha256(value.encode("utf-8")).hexdigest()

    def test_different_strings_give_different_hashes(self):
        assert calibrate.hash_output("a") != calibrate.hash_output("b")

    def test_trimmed_and_untrimmed_strings_differ(self):
        # Documents the "no trimming" normalization rule explicitly.
        assert calibrate.hash_output("value") != calibrate.hash_output(" value ")


# --------------------------------------------------------------------------
# labeling_template_rows
# --------------------------------------------------------------------------


class TestLabelingTemplateRows:
    def test_emits_one_prefilled_row_per_triple_with_correct_hash(self):
        email = make_item("cal-001").email
        triples = [
            Triple("cal-001", "a", "issue_summary", email, "ref-1", "candidate-value-1"),
            Triple("cal-001", "a", "requested_action", email, "ref-2", "candidate-value-2"),
        ]

        rows = calibrate.labeling_template_rows(triples)

        assert len(rows) == 2
        assert rows[0] == {
            "item_id": "cal-001",
            "candidate": "a",
            "field": "issue_summary",
            "candidate_value": "candidate-value-1",
            "output_sha256": calibrate.hash_output("candidate-value-1"),
            "verdict": "",
            "critique": "",
        }
        assert rows[1]["output_sha256"] == calibrate.hash_output("candidate-value-2")

    def test_empty_triples_gives_empty_rows(self):
        assert calibrate.labeling_template_rows([]) == []

    def test_rows_are_born_correctly_bound_for_calibration_label(self):
        """Every row's output_sha256 must equal hash_output(candidate_value)
        exactly, since a future generator will feed candidate_value/
        output_sha256 straight into a CalibrationLabel once verdict/critique
        are filled in by hand."""

        email = make_item("cal-001").email
        triples = [Triple("cal-001", "b", "issue_summary", email, "ref", "some output text")]

        rows = calibrate.labeling_template_rows(triples)

        assert rows[0]["output_sha256"] == calibrate.hash_output(rows[0]["candidate_value"])


# --------------------------------------------------------------------------
# compute_agreement
# --------------------------------------------------------------------------


class TestComputeAgreement:
    def _perfect_agreement_fixture(self) -> list[PairedJudgment]:
        # 6 clusters (emails), one fail pair each for "a" and "b" so neither
        # candidate's subset is single-category; otherwise all pass. Perfect
        # agreement everywhere -> every well-defined kappa is exactly 1.0.
        paired = []
        for i in range(1, 7):
            item_id = f"cal-{i:03d}"
            for candidate in ("a", "b"):
                verdict = "fail" if (item_id, candidate) == ("cal-002", "a") or (
                    item_id,
                    candidate,
                ) == ("cal-004", "b") else "pass"
                paired.append(
                    PairedJudgment(
                        item_id=item_id,
                        candidate=candidate,
                        owner_verdict=verdict,
                        judge_verdict=verdict,
                    )
                )
        return paired

    def test_perfect_agreement_gives_kappa_one_overall_and_per_candidate(self):
        paired = self._perfect_agreement_fixture()

        overall, per_candidate, warnings_out = compute_agreement(
            paired, n_resamples=200, seed=0
        )

        assert overall.kappa == pytest.approx(1.0)
        assert overall.ci == pytest.approx((1.0, 1.0))
        assert set(per_candidate) == {"a", "b"}
        assert per_candidate["a"].kappa == pytest.approx(1.0)
        assert per_candidate["b"].kappa == pytest.approx(1.0)
        assert isinstance(warnings_out, tuple)

    def test_clusters_are_item_ids_not_flat_positions(self, monkeypatch):
        captured_clusters: list[list[str]] = []
        real_cohens_kappa = agreement_module.cohens_kappa

        def _spy(a, b, *, clusters=None, **kwargs):
            captured_clusters.append(list(clusters))
            return real_cohens_kappa(a, b, clusters=clusters, **kwargs)

        monkeypatch.setattr(calibrate, "cohens_kappa", _spy)
        paired = self._perfect_agreement_fixture()

        compute_agreement(paired, n_resamples=50, seed=0)

        assert captured_clusters[0] == [p.item_id for p in paired]

    def test_captures_and_forwards_runtime_warning(self, monkeypatch):
        """calibrate.py must not let cohens_kappa's degenerate-resample
        disclosure propagate silently -- it captures the RuntimeWarning and
        returns its message so the report can render it (D2 disclosure)."""

        def _fake_cohens_kappa(a, b, *, clusters=None, level=0.95, n_resamples=10_000, seed=0):
            warnings.warn(
                "Omitted 3 of 10 bootstrap replicates (fixture)", RuntimeWarning, stacklevel=2
            )
            return KappaResult(kappa=0.8, ci=(0.5, 0.95), raw_agreement=0.9, prevalence=0.5)

        monkeypatch.setattr(calibrate, "cohens_kappa", _fake_cohens_kappa)
        paired = [
            PairedJudgment("cal-001", "a", "pass", "pass"),
            PairedJudgment("cal-002", "a", "fail", "fail"),
        ]

        with warnings.catch_warnings():
            warnings.simplefilter("error")  # would raise if the warning escapes uncaptured
            overall, per_candidate, warnings_out = compute_agreement(paired)

        assert overall.kappa == 0.8
        assert any("Omitted 3 of 10" in w for w in warnings_out)


# --------------------------------------------------------------------------
# decide_verdict / per_candidate_divergence_flag
# --------------------------------------------------------------------------


class TestDecideVerdict:
    def test_kappa_ge_06_and_ci_lower_ge_04_is_adequate(self):
        assert decide_verdict(0.72, (0.55, 0.85)) == "adequate"

    def test_kappa_ge_06_but_ci_lower_below_04_is_adequate_with_caveat(self):
        assert decide_verdict(0.62, (0.25, 0.8)) == "adequate_with_caveat"

    def test_kappa_below_06_is_inadequate(self):
        assert decide_verdict(0.35, (0.1, 0.55)) == "inadequate"

    def test_boundary_kappa_exactly_06_with_ci_lower_exactly_04_is_adequate(self):
        assert decide_verdict(0.6, (0.4, 0.8)) == "adequate"

    def test_boundary_ci_lower_just_below_04_is_caveat(self):
        assert decide_verdict(0.6, (0.399, 0.8)) == "adequate_with_caveat"


class TestPerCandidateDivergenceFlag:
    def test_gap_above_threshold_flags(self):
        assert per_candidate_divergence_flag({"a": 0.8, "b": 0.5}) is True

    def test_gap_below_threshold_does_not_flag(self):
        assert per_candidate_divergence_flag({"a": 0.75, "b": 0.6}) is False

    def test_nan_kappa_excluded_from_comparison(self):
        assert per_candidate_divergence_flag({"a": float("nan"), "b": 0.7}) is False

    def test_single_candidate_never_flags(self):
        assert per_candidate_divergence_flag({"a": 0.9}) is False


# --------------------------------------------------------------------------
# Self-consistency.
# --------------------------------------------------------------------------


class TestSelectFixedSelfConsistencyTriples:
    def test_deterministic_prefix_sorted_by_item_candidate_field(self):
        email = make_item("x").email
        triples = [
            Triple("cal-002", "b", "requested_action", email, "r", "v"),
            Triple("cal-001", "b", "issue_summary", email, "r", "v"),
            Triple("cal-001", "a", "issue_summary", email, "r", "v"),
        ]

        selected = select_fixed_self_consistency_triples(triples, n=2)

        assert [(t.item_id, t.candidate, t.field) for t in selected] == [
            ("cal-001", "a", "issue_summary"),
            ("cal-001", "b", "issue_summary"),
        ]

    def test_returns_fewer_than_n_when_not_enough_available(self):
        email = make_item("x").email
        triples = [Triple("cal-001", "a", "issue_summary", email, "r", "v")]

        assert len(select_fixed_self_consistency_triples(triples, n=20)) == 1


class TestMeasureSelfConsistency:
    def test_exactly_one_flipping_triple_gives_flip_rate_1_of_20(self):
        email = make_item("x").email
        triples = [
            Triple(f"cal-{i:03d}", "a", "issue_summary", email, "ref", f"val-{i}")
            for i in range(20)
        ]
        flipping_value = "val-7"

        def verdict_for(candidate_value: str, idx: int) -> str:
            if candidate_value == flipping_value:
                return "pass" if idx < 2 else "fail"
            return "pass"

        judge = _judge(verdict_for)

        result = measure_self_consistency(judge, triples, repeats=3)

        assert result.n_triples == 20
        assert result.repeats == 3
        assert result.flip_rate == pytest.approx(1 / 20)
        assert result.flipped_triples == (("cal-007", "a", "issue_summary"),)

    def test_no_flips_gives_zero_flip_rate(self):
        email = make_item("x").email
        triples = [
            Triple(f"cal-{i:03d}", "a", "issue_summary", email, "ref", f"val-{i}")
            for i in range(20)
        ]
        judge = _judge(lambda cv, idx: "pass")

        result = measure_self_consistency(judge, triples, repeats=3)

        assert result.flip_rate == 0.0
        assert result.flipped_triples == ()

    def test_judge_errors_are_not_treated_as_flips(self):
        email = make_item("x").email
        triples = [Triple("cal-001", "a", "issue_summary", email, "ref", "val")]
        # Every repeat errors -> fewer than 2 determinate verdicts -> no flip possible.
        judge = _judge(lambda cv, idx: "__error__")

        result = measure_self_consistency(judge, triples, repeats=3)

        assert result.flip_rate == 0.0


# --------------------------------------------------------------------------
# Test-retest ceiling.
# --------------------------------------------------------------------------


class TestComputeRetestCeiling:
    def test_perfect_agreement_on_intersection_gives_kappa_one(self):
        labels = []
        for i in range(1, 7):
            item_id = f"cal-{i:03d}"
            verdict = "fail" if item_id == "cal-002" else "pass"
            labels.append(make_label(item_id, "a", "issue_summary", verdict, round_="initial"))
            labels.append(make_label(item_id, "a", "issue_summary", verdict, round_="retest"))

        ceiling, warnings_out = compute_retest_ceiling(labels, n_resamples=200, seed=0)

        assert ceiling is not None
        assert ceiling.kappa == pytest.approx(1.0)
        assert isinstance(warnings_out, tuple)

    def test_fewer_than_two_shared_keys_returns_none(self):
        labels = [
            make_label("cal-001", "a", "issue_summary", "pass", round_="initial"),
            make_label("cal-001", "a", "issue_summary", "pass", round_="retest"),
        ]

        ceiling, warnings_out = compute_retest_ceiling(labels)

        assert ceiling is None
        assert warnings_out == ()

    def test_no_retest_labels_returns_none(self):
        labels = [make_label("cal-001", "a", "issue_summary", "pass", round_="initial")]

        ceiling, _ = compute_retest_ceiling(labels)

        assert ceiling is None

    def test_matching_output_sha256_across_rounds_allows_ceiling(self):
        """When initial and retest labels for the same key have matching
        output_sha256 (labeled the same candidate output), ceiling is computed
        normally."""
        labels = []
        for i in range(1, 7):  # Need at least 2 shared keys; use 6 for variance
            item_id = f"cal-{i:03d}"
            verdict = "fail" if item_id == "cal-002" else "pass"
            # Both rounds label the same candidate value -> same output_sha256
            labels.append(
                make_label(
                    item_id,
                    "a",
                    "issue_summary",
                    verdict,
                    round_="initial",
                    candidate_value="same-value",
                )
            )
            labels.append(
                make_label(
                    item_id,
                    "a",
                    "issue_summary",
                    verdict,
                    round_="retest",
                    candidate_value="same-value",
                )
            )

        ceiling, warnings_out = compute_retest_ceiling(labels, n_resamples=200, seed=0)

        assert ceiling is not None
        assert ceiling.kappa == pytest.approx(1.0)

    def test_mismatched_output_sha256_raises_binding_error(self):
        """When initial and retest labels for the same key have different
        output_sha256 (labeled different candidate outputs), raises
        CalibrationBindingError all-or-nothing (no partial ceiling)."""
        labels = []
        for i in range(1, 3):
            item_id = f"cal-{i:03d}"
            # Initial labels one candidate value
            labels.append(
                make_label(
                    item_id,
                    "a",
                    "issue_summary",
                    "pass",
                    round_="initial",
                    candidate_value="initial-value",
                )
            )
            # Retest labels a different candidate value
            labels.append(
                make_label(
                    item_id,
                    "a",
                    "issue_summary",
                    "pass",
                    round_="retest",
                    candidate_value="retest-value",
                )
            )

        with pytest.raises(calibrate.CalibrationBindingError) as excinfo:
            compute_retest_ceiling(labels)

        assert len(excinfo.value.mismatches) == 2
        assert ("cal-001", "a", "issue_summary") in excinfo.value.mismatches
        assert ("cal-002", "a", "issue_summary") in excinfo.value.mismatches

    def test_one_mismatched_key_blocks_all_ceiling_computation(self):
        """All-or-nothing: a single mismatched output_sha256 prevents the
        entire ceiling from being computed, even if other keys match."""
        good_value = "good-candidate-value"
        bad_value = "bad-candidate-value"
        labels = [
            # This key matches
            make_label(
                "cal-001",
                "a",
                "issue_summary",
                "pass",
                round_="initial",
                candidate_value=good_value,
            ),
            make_label(
                "cal-001",
                "a",
                "issue_summary",
                "pass",
                round_="retest",
                candidate_value=good_value,
            ),
            # This key mismatches
            make_label(
                "cal-002",
                "a",
                "issue_summary",
                "pass",
                round_="initial",
                candidate_value=good_value,
            ),
            make_label(
                "cal-002",
                "a",
                "issue_summary",
                "pass",
                round_="retest",
                candidate_value=bad_value,
            ),
        ]

        with pytest.raises(calibrate.CalibrationBindingError) as excinfo:
            compute_retest_ceiling(labels)

        # Only the mismatched key should be reported
        assert excinfo.value.mismatches == (("cal-002", "a", "issue_summary"),)


# --------------------------------------------------------------------------
# Disjointness from golden.
# --------------------------------------------------------------------------


class TestCheckDisjointFromGolden:
    def test_disjoint_items_pass(self):
        calibration_items = [make_item("cal-001"), make_item("cal-002")]
        golden_items = [make_item("gold-001"), make_item("gold-002")]

        check_disjoint_from_golden(calibration_items, golden_items)  # no raise

    def test_shared_id_raises(self):
        calibration_items = [make_item("shared-001")]
        golden_items = [make_item("shared-001")]

        with pytest.raises(ValueError, match="overlap"):
            check_disjoint_from_golden(calibration_items, golden_items)

    def test_shared_email_content_with_different_id_raises(self):
        golden_items = [make_item("gold-001")]
        calibration_item = make_item("cal-001")
        # Force identical (subject, body) content under a distinct id.
        duplicate = calibration_item.model_copy(update={"email": golden_items[0].email})

        with pytest.raises(ValueError, match="overlap"):
            check_disjoint_from_golden([duplicate], golden_items)


class TestRealCalibrationDatasetDisjointness:
    """Ticket AC: ``data/calibration/emails.jsonl`` must be disjoint from
    ``data/golden/golden.jsonl``. Both are owner/T13 deliverables that don't
    exist yet at this ticket's CODE-only scope (golden set not yet frozen,
    calibration emails not yet authored) -- this SKIPs gracefully until both
    land, mirroring ``test_golden_dataset.py``'s convention, and then enforces
    the freeze contract for real."""

    _REPO_ROOT = Path(__file__).parents[2]
    _EMAILS_PATH = _REPO_ROOT / "data" / "calibration" / "emails.jsonl"
    _GOLDEN_PATH = _REPO_ROOT / "data" / "golden" / "golden.jsonl"

    def test_real_calibration_emails_are_disjoint_from_golden(self):
        if not self._EMAILS_PATH.is_file() or not self._GOLDEN_PATH.is_file():
            pytest.skip(
                "data/calibration/emails.jsonl and/or data/golden/golden.jsonl do not exist "
                "yet -- both are authored after this ticket's code-only scope. This test "
                "enforces the disjointness contract once both land."
            )
        def _load(path: Path) -> list[GoldenItem]:
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            return [GoldenItem.model_validate(json.loads(ln)) for ln in lines]

        check_disjoint_from_golden(_load(self._EMAILS_PATH), _load(self._GOLDEN_PATH))


# --------------------------------------------------------------------------
# resolve_certificate_date / hash_label_file / load_calibration_labels.
# --------------------------------------------------------------------------


class TestResolveCertificateDate:
    def test_explicit_date_wins(self):
        labels = [make_label("cal-001", "a", "issue_summary", "pass", label_date_="2026-01-01")]

        result = resolve_certificate_date(labels, explicit=date(2027, 5, 5))

        assert result == date(2027, 5, 5)

    def test_defaults_to_most_recent_label_date_across_any_round(self):
        labels = [
            make_label("cal-001", "a", "issue_summary", "pass", label_date_="2026-01-01"),
            make_label(
                "cal-001",
                "a",
                "issue_summary",
                "pass",
                round_="retest",
                label_date_="2026-06-15",
            ),
            make_label("cal-002", "a", "issue_summary", "pass", label_date_="2026-03-01"),
        ]

        result = resolve_certificate_date(labels)

        assert result == date(2026, 6, 15)

    def test_raises_with_no_labels_and_no_explicit_date(self):
        with pytest.raises(ValueError):
            resolve_certificate_date([])


class TestHashLabelFile:
    def test_hash_matches_manual_sha256(self, tmp_path):
        import hashlib

        path = tmp_path / "labels.jsonl"
        path.write_text('{"a": 1}\n', encoding="utf-8")

        assert hash_label_file(path) == hashlib.sha256(path.read_bytes()).hexdigest()

    def test_different_content_gives_different_hash(self, tmp_path):
        path_a = tmp_path / "a.jsonl"
        path_b = tmp_path / "b.jsonl"
        path_a.write_text("one\n", encoding="utf-8")
        path_b.write_text("two\n", encoding="utf-8")

        assert hash_label_file(path_a) != hash_label_file(path_b)


class TestLoadCalibrationLabels:
    def test_round_trips_jsonl(self, tmp_path):
        path = tmp_path / "labels.jsonl"
        rows = [
            make_label("cal-001", "a", "issue_summary", "pass").model_dump(mode="json"),
            make_label("cal-001", "b", "requested_action", "fail").model_dump(mode="json"),
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

        labels = load_calibration_labels(path)

        assert len(labels) == 2
        assert all(isinstance(label, CalibrationLabel) for label in labels)

    def test_skips_blank_lines(self, tmp_path):
        path = tmp_path / "labels.jsonl"
        row = make_label("cal-001", "a", "issue_summary", "pass").model_dump(mode="json")
        path.write_text(f"\n{json.dumps(row)}\n\n", encoding="utf-8")

        labels = load_calibration_labels(path)

        assert len(labels) == 1


# --------------------------------------------------------------------------
# build_certificate / write_certificate round trip.
# --------------------------------------------------------------------------


def _sample_result(**overrides) -> CalibrationResult:
    defaults = dict(
        judge_version="jv-1",
        label_file_hash="hash-1",
        date=date(2026, 6, 1),
        overall=KappaResult(kappa=0.72, ci=(0.55, 0.85), raw_agreement=0.9, prevalence=0.6),
        per_candidate={
            "a": KappaResult(kappa=0.7, ci=(0.5, 0.86), raw_agreement=0.9, prevalence=0.6),
            "b": KappaResult(kappa=0.74, ci=(0.58, 0.88), raw_agreement=0.91, prevalence=0.6),
        },
        verdict="adequate",
        divergence_flag=False,
        initial_fail_rate=0.25,
        fail_enrichment_note=False,
        judge_errors_excluded=0,
        unlabeled_excluded=0,
        self_consistency=SelfConsistencyResult(
            n_triples=20,
            repeats=3,
            flip_rate=0.05,
            flipped_triples=(("cal-007", "a", "issue_summary"),),
        ),
        ceiling=None,
        warnings=(),
    )
    defaults.update(overrides)
    return CalibrationResult(**defaults)


class TestBuildCertificate:
    def test_populates_every_spec_field_including_per_candidate_ci(self):
        result = _sample_result()

        certificate = build_certificate(result)

        assert isinstance(certificate, Certificate)
        assert certificate.judge_version == "jv-1"
        assert certificate.overall_kappa == pytest.approx(0.72)
        assert certificate.kappa_ci == pytest.approx((0.55, 0.85))
        assert certificate.per_candidate_kappa == {
            "a": pytest.approx(0.7),
            "b": pytest.approx(0.74),
        }
        assert certificate.per_candidate_kappa_ci == {
            "a": pytest.approx((0.5, 0.86)),
            "b": pytest.approx((0.58, 0.88)),
        }
        assert certificate.verdict == "adequate"
        assert certificate.ceiling_kappa is None
        assert certificate.label_file_hash == "hash-1"
        assert certificate.date == date(2026, 6, 1)

    def test_ceiling_kappa_populated_when_result_has_ceiling(self):
        result = _sample_result(
            ceiling=KappaResult(kappa=0.9, ci=(0.8, 0.95), raw_agreement=0.95, prevalence=0.6)
        )

        certificate = build_certificate(result)

        assert certificate.ceiling_kappa == pytest.approx(0.9)


class TestWriteCertificateRoundTrip:
    def test_json_round_trip(self, tmp_path):
        result = _sample_result()
        certificate = build_certificate(result)
        path = tmp_path / "data" / "calibration" / "certificate.json"

        write_certificate(certificate, path)

        loaded = Certificate.model_validate(json.loads(path.read_text(encoding="utf-8")))
        assert loaded == certificate


# --------------------------------------------------------------------------
# render_calibration_report.
# --------------------------------------------------------------------------


class TestRenderCalibrationReport:
    def test_renders_adequate_verdict_without_caveat_line(self):
        actual = render_calibration_report(_sample_result(verdict="adequate"))

        assert "Verdict: **adequate**" in actual
        assert "Gray zone" not in actual

    def test_renders_adequate_with_caveat_line(self):
        actual = render_calibration_report(_sample_result(verdict="adequate_with_caveat"))

        assert "Verdict: **adequate_with_caveat**" in actual
        assert "Gray zone" in actual

    def test_renders_inadequate_verdict(self):
        actual = render_calibration_report(_sample_result(verdict="inadequate"))

        assert "Verdict: **inadequate**" in actual

    def test_renders_d1_review_flag_when_divergence_true(self):
        actual = render_calibration_report(_sample_result(divergence_flag=True))

        assert "D1-review flag" in actual
        assert "never a gate condition" in actual

    def test_omits_d1_review_flag_when_divergence_false(self):
        actual = render_calibration_report(_sample_result(divergence_flag=False))

        assert "D1-review flag" not in actual

    def test_renders_fail_enrichment_note_when_flagged(self):
        actual = render_calibration_report(
            _sample_result(initial_fail_rate=0.1, fail_enrichment_note=True)
        )

        assert "10.0%" in actual
        assert "harder-than-operational distribution" in actual

    def test_omits_fail_enrichment_note_when_not_flagged(self):
        actual = render_calibration_report(
            _sample_result(initial_fail_rate=0.3, fail_enrichment_note=False)
        )

        assert "harder-than-operational distribution" not in actual

    def test_renders_self_consistency_flip_rate(self):
        actual = render_calibration_report(_sample_result())

        assert "20 fixed" in actual
        assert "flip rate = 5.0%" in actual
        assert "(1/20)" in actual

    def test_omits_ceiling_section_when_absent(self):
        actual = render_calibration_report(_sample_result(ceiling=None))

        assert "Test-Retest Consistency Ceiling" not in actual

    def test_renders_ceiling_section_labeled_as_estimate(self):
        result = _sample_result(
            ceiling=KappaResult(kappa=0.9, ci=(0.8, 0.95), raw_agreement=0.95, prevalence=0.6)
        )

        actual = render_calibration_report(result)

        assert "Test-Retest Consistency Ceiling" in actual
        assert "an estimate of the consistency ceiling" in actual

    def test_renders_bootstrap_disclosures_when_present(self):
        result = _sample_result(warnings=("Omitted 5 of 10000 bootstrap replicates (fixture)",))

        actual = render_calibration_report(result)

        assert "Bootstrap Disclosures" in actual
        assert "Omitted 5 of 10000 bootstrap replicates (fixture)" in actual

    def test_omits_bootstrap_disclosures_section_when_no_warnings(self):
        actual = render_calibration_report(_sample_result(warnings=()))

        assert "Bootstrap Disclosures" not in actual

    def test_excluded_counts_rendered(self):
        actual = render_calibration_report(
            _sample_result(judge_errors_excluded=2, unlabeled_excluded=3)
        )

        assert "2 judge error(s)" in actual
        assert "3 judged field(s)" in actual


# --------------------------------------------------------------------------
# run_calibration end-to-end (synthetic RunArtifacts + fake judge).
# --------------------------------------------------------------------------


def _calibration_fixture():
    """6 calibration items, 2 candidates, 2 fields = 24 triples total.
    Designed for exact, hand-verifiable agreement: one unlabeled triple, one
    judge-error triple, and perfect (owner-label == judge-verdict) agreement
    everywhere else, with one "fail" pair per candidate so neither candidate's
    subset collapses to a single category.
    """

    item_ids = [f"cal-{i:03d}" for i in range(1, 7)]
    items = [make_item(item_id) for item_id in item_ids]

    def cv(item_id: str, candidate: str, field: str) -> str:
        return f"{item_id}-{candidate}-{field}-value"

    rows_by_candidate: dict[str, list[RunRow]] = {"a": [], "b": []}
    for item_id in item_ids:
        for candidate in ("a", "b"):
            rows_by_candidate[candidate].append(
                make_row(
                    item_id,
                    0,
                    issue_summary=cv(item_id, candidate, "issue_summary"),
                    requested_action=cv(item_id, candidate, "requested_action"),
                )
            )

    run_a = make_run_artifact("a", items, rows_by_candidate["a"])
    run_b = make_run_artifact("b", items, rows_by_candidate["b"])

    fail_pairs = {("cal-002", "a", "issue_summary"), ("cal-004", "b", "requested_action")}
    unlabeled = ("cal-005", "a", "requested_action")
    judge_error = ("cal-006", "b", "issue_summary")

    labels: list[CalibrationLabel] = []
    verdict_table: dict[str, str] = {}
    for item_id in item_ids:
        for candidate in ("a", "b"):
            for f in ("issue_summary", "requested_action"):
                key = (item_id, candidate, f)
                value = cv(item_id, candidate, f)
                label_verdict = "fail" if key in fail_pairs else "pass"
                if key != unlabeled:
                    labels.append(
                        make_label(item_id, candidate, f, label_verdict, candidate_value=value)
                    )
                # Every triple is judged regardless of whether it ends up
                # labeled -- judge_triples judges the full triple set, and
                # pairing (not judging) is what excludes the unlabeled one.
                if key == judge_error:
                    verdict_table[value] = "__error__"
                else:
                    verdict_table[value] = label_verdict  # judge agrees with the owner

    judge = _judge(lambda candidate_value, idx: verdict_table[candidate_value])
    return run_a, run_b, labels, judge


class TestRunCalibrationIntegration:
    def test_full_pipeline_perfect_agreement_is_adequate(self):
        run_a, run_b, labels, judge = _calibration_fixture()

        result = run_calibration(
            run_a=run_a,
            run_b=run_b,
            labels=labels,
            judge=judge,
            label_file_hash="fixture-hash",
            n_resamples=200,
            seed=0,
        )

        assert result.judge_version == judge_version()
        assert result.overall.kappa == pytest.approx(1.0)
        assert result.verdict == "adequate"
        assert result.divergence_flag is False
        assert result.judge_errors_excluded == 1
        assert result.unlabeled_excluded == 1
        assert set(result.per_candidate) == {"a", "b"}
        assert result.per_candidate["a"].kappa == pytest.approx(1.0)
        assert result.per_candidate["b"].kappa == pytest.approx(1.0)
        assert result.self_consistency.n_triples == 20  # default self-consistency n
        assert result.ceiling is None
        assert result.date == max(label.label_date for label in labels)

    def test_date_override_wins_over_label_dates(self):
        run_a, run_b, labels, judge = _calibration_fixture()

        result = run_calibration(
            run_a=run_a,
            run_b=run_b,
            labels=labels,
            judge=judge,
            label_file_hash="fixture-hash",
            date_override=date(2030, 1, 1),
            n_resamples=50,
            seed=0,
        )

        assert result.date == date(2030, 1, 1)

    def test_retest_flag_adds_ceiling_from_retest_labels(self):
        run_a, run_b, labels, judge = _calibration_fixture()
        # Add retest labels for a subset of the initial keys, perfectly consistent.
        # Must use same candidate_value as initial labels (output_sha256 binding).
        def cv(item_id: str, candidate: str, field: str) -> str:
            return f"{item_id}-{candidate}-{field}-value"

        retest_labels = [
            make_label(
                "cal-001",
                "a",
                "issue_summary",
                "pass",
                round_="retest",
                candidate_value=cv("cal-001", "a", "issue_summary"),
            ),
            make_label(
                "cal-002",
                "a",
                "issue_summary",
                "fail",
                round_="retest",
                candidate_value=cv("cal-002", "a", "issue_summary"),
            ),
            make_label(
                "cal-003",
                "a",
                "issue_summary",
                "pass",
                round_="retest",
                candidate_value=cv("cal-003", "a", "issue_summary"),
            ),
        ]

        result = run_calibration(
            run_a=run_a,
            run_b=run_b,
            labels=labels + retest_labels,
            judge=judge,
            label_file_hash="fixture-hash",
            n_resamples=50,
            seed=0,
            retest=True,
        )

        assert result.ceiling is not None
        assert result.ceiling.kappa == pytest.approx(1.0)
        report = render_calibration_report(result)
        assert "an estimate of the consistency ceiling" in report

    def test_retest_false_never_computes_ceiling_even_with_retest_labels_present(self):
        run_a, run_b, labels, judge = _calibration_fixture()
        # Must use same candidate_value as initial labels (output_sha256 binding).
        def cv(item_id: str, candidate: str, field: str) -> str:
            return f"{item_id}-{candidate}-{field}-value"

        retest_labels = [
            make_label(
                "cal-001",
                "a",
                "issue_summary",
                "pass",
                round_="retest",
                candidate_value=cv("cal-001", "a", "issue_summary"),
            ),
            make_label(
                "cal-002",
                "a",
                "issue_summary",
                "fail",
                round_="retest",
                candidate_value=cv("cal-002", "a", "issue_summary"),
            ),
        ]

        result = run_calibration(
            run_a=run_a,
            run_b=run_b,
            labels=labels + retest_labels,
            judge=judge,
            label_file_hash="fixture-hash",
            n_resamples=50,
            seed=0,
            retest=False,
        )

        assert result.ceiling is None

    def test_no_matching_labels_raises(self):
        run_a, run_b, _labels, judge = _calibration_fixture()

        with pytest.raises(ValueError, match="no labeled triple"):
            run_calibration(
                run_a=run_a,
                run_b=run_b,
                labels=[],
                judge=judge,
                label_file_hash="fixture-hash",
                n_resamples=50,
            )


# --------------------------------------------------------------------------
# judgment_records_from_judged / pair_judgments_with_labels (finding F2)
# --------------------------------------------------------------------------


class TestJudgmentRecordsFromJudged:
    def test_converts_judged_triples_with_correct_hashes(self):
        email = make_item("cal-001").email
        triple = Triple("cal-001", "a", "issue_summary", email, "ref", "the-value")
        judged = [JudgedTriple(triple, verdict="pass", error=None, rationale="ok")]

        records = calibrate.judgment_records_from_judged(judged, judge_version="jv-x")

        assert len(records) == 1
        record = records[0]
        assert record.item_id == "cal-001"
        assert record.candidate == "a"
        assert record.field == "issue_summary"
        assert record.verdict == "pass"
        assert record.error is None
        assert record.rationale == "ok"
        assert record.output_sha256 == calibrate.hash_output("the-value")
        assert record.judge_version == "jv-x"

    def test_preserves_error_and_none_verdict(self):
        email = make_item("cal-001").email
        triple = Triple("cal-001", "b", "requested_action", email, "ref", "some-value")
        judged = [JudgedTriple(triple, verdict=None, error="refusal", rationale=None)]

        records = calibrate.judgment_records_from_judged(judged, judge_version="jv-x")

        assert records[0].verdict is None
        assert records[0].error == "refusal"


class TestPairJudgmentsWithLabels:
    def test_pairs_matching_and_excludes_judge_errors_and_unlabeled(self):
        judgments = [
            calibrate.JudgmentRecord(
                "cal-001", "a", "issue_summary", "pass", None, "ok",
                calibrate.hash_output("v1"), "jv",
            ),
            calibrate.JudgmentRecord(
                "cal-001", "a", "requested_action", None, "refusal", None,
                calibrate.hash_output("v2"), "jv",
            ),
            calibrate.JudgmentRecord(
                "cal-002", "a", "issue_summary", "pass", None, "ok",
                calibrate.hash_output("v3"), "jv",
            ),
        ]
        labels = [
            make_label("cal-001", "a", "issue_summary", "pass", candidate_value="v1"),
            make_label("cal-001", "a", "requested_action", "pass", candidate_value="v2"),
        ]

        paired, judge_errors, unlabeled = calibrate.pair_judgments_with_labels(
            judgments, labels, round_="initial"
        )

        assert len(paired) == 1
        assert judge_errors == 1
        assert unlabeled == 1

    def test_persisted_hash_mismatch_raises_binding_error(self):
        judgments = [
            calibrate.JudgmentRecord(
                "cal-001", "a", "issue_summary", "pass", None, "ok", "deadbeef" * 8, "jv",
            ),
        ]
        labels = [make_label("cal-001", "a", "issue_summary", "pass", candidate_value="v1")]

        with pytest.raises(calibrate.CalibrationBindingError) as excinfo:
            calibrate.pair_judgments_with_labels(judgments, labels, round_="initial")

        assert excinfo.value.mismatches == (("cal-001", "a", "issue_summary"),)


# --------------------------------------------------------------------------
# write_judgments_jsonl / load_judgments_jsonl round trip (finding F2)
# --------------------------------------------------------------------------


class TestJudgmentsFileRoundTrip:
    def test_round_trips_meta_judgments_and_self_consistency(self, tmp_path):
        path = tmp_path / "judgments.jsonl"
        judgments = [
            calibrate.JudgmentRecord(
                "cal-001", "a", "issue_summary", "pass", None, "ok",
                calibrate.hash_output("v1"), "jv-1",
            ),
            calibrate.JudgmentRecord(
                "cal-002", "b", "requested_action", None, "refusal", None,
                calibrate.hash_output("v2"), "jv-1",
            ),
        ]
        self_consistency = [
            calibrate.SelfConsistencyRecord("cal-001", "a", "issue_summary", 0, "pass", "jv-1"),
            calibrate.SelfConsistencyRecord("cal-001", "a", "issue_summary", 1, "fail", "jv-1"),
        ]

        calibrate.write_judgments_jsonl(
            path,
            judgments=judgments,
            self_consistency=self_consistency,
            judge_version="jv-1",
            written_at="2026-06-01T00:00:00+00:00",
        )
        loaded = calibrate.load_judgments_jsonl(path)

        assert loaded.judge_version == "jv-1"
        assert loaded.written_at == "2026-06-01T00:00:00+00:00"
        assert loaded.judgments == tuple(judgments)
        assert loaded.self_consistency == tuple(self_consistency)

    def test_write_is_atomic_no_leftover_temp_file(self, tmp_path):
        path = tmp_path / "judgments.jsonl"

        calibrate.write_judgments_jsonl(
            path, judgments=[], self_consistency=[], judge_version="jv-1"
        )

        assert path.exists()
        assert not (tmp_path / "judgments.jsonl.tmp").exists()

    def test_overwrite_fully_replaces_prior_content(self, tmp_path):
        path = tmp_path / "judgments.jsonl"
        calibrate.write_judgments_jsonl(
            path,
            judgments=[
                calibrate.JudgmentRecord(
                    "cal-001", "a", "issue_summary", "pass", None, "ok", "h1", "jv-1"
                )
            ],
            self_consistency=[],
            judge_version="jv-1",
        )

        calibrate.write_judgments_jsonl(
            path, judgments=[], self_consistency=[], judge_version="jv-2"
        )

        loaded = calibrate.load_judgments_jsonl(path)
        assert loaded.judge_version == "jv-2"
        assert loaded.judgments == ()

    def test_missing_meta_row_raises(self, tmp_path):
        path = tmp_path / "judgments.jsonl"
        row = {
            "kind": "judgment",
            "item_id": "x",
            "candidate": "a",
            "field": "issue_summary",
            "verdict": "pass",
            "error": None,
            "rationale": "ok",
            "output_sha256": "h",
            "judge_version": "jv",
        }
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")

        with pytest.raises(ValueError, match="meta"):
            calibrate.load_judgments_jsonl(path)

    def test_unrecognized_row_kind_raises(self, tmp_path):
        path = tmp_path / "judgments.jsonl"
        lines = [
            json.dumps({"kind": "meta", "judge_version": "jv-1", "written_at": "2026-01-01"}),
            json.dumps({"kind": "mystery"}),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with pytest.raises(ValueError, match="unrecognized"):
            calibrate.load_judgments_jsonl(path)


# --------------------------------------------------------------------------
# run_calibration_offline: zero-API recompute (finding F2)
# --------------------------------------------------------------------------


class TestRunCalibrationOffline:
    def _judgments_file_from_live_result(
        self, result: CalibrationResult
    ) -> calibrate.JudgmentsFile:
        return calibrate.JudgmentsFile(
            judge_version=result.judge_version,
            written_at="2026-06-01T00:00:00+00:00",
            judgments=tuple(
                calibrate.judgment_records_from_judged(
                    result.judged_triples, judge_version=result.judge_version
                )
            ),
            self_consistency=result.self_consistency_records,
        )

    def test_offline_recompute_matches_live_result(self):
        run_a, run_b, labels, judge = _calibration_fixture()
        live_result = run_calibration(
            run_a=run_a,
            run_b=run_b,
            labels=labels,
            judge=judge,
            label_file_hash="fixture-hash",
            n_resamples=200,
            seed=0,
        )
        judgments_file = self._judgments_file_from_live_result(live_result)

        offline_result = calibrate.run_calibration_offline(
            judgments=judgments_file,
            labels=labels,
            label_file_hash="fixture-hash",
            n_resamples=200,
            seed=0,
        )

        assert offline_result.overall.kappa == pytest.approx(live_result.overall.kappa)
        assert offline_result.overall.ci == pytest.approx(live_result.overall.ci)
        assert offline_result.verdict == live_result.verdict
        assert offline_result.judge_errors_excluded == live_result.judge_errors_excluded
        assert offline_result.unlabeled_excluded == live_result.unlabeled_excluded
        assert offline_result.self_consistency.n_triples == live_result.self_consistency.n_triples
        assert offline_result.self_consistency.flip_rate == pytest.approx(
            live_result.self_consistency.flip_rate
        )
        assert offline_result.date == live_result.date
        # Offline never re-derives judged_triples/self_consistency_records --
        # it already consumed a persisted copy of them.
        assert offline_result.judged_triples == ()
        assert offline_result.self_consistency_records == ()

    def test_offline_never_touches_judge_or_client(self):
        """No ``judge``/``Judge`` argument even exists on this call --
        proves by signature, not just by absence of a fake, that zero calls
        can be made."""

        run_a, run_b, labels, judge = _calibration_fixture()
        live_result = run_calibration(
            run_a=run_a,
            run_b=run_b,
            labels=labels,
            judge=judge,
            label_file_hash="fixture-hash",
            n_resamples=50,
            seed=0,
        )
        judgments_file = self._judgments_file_from_live_result(live_result)

        import inspect

        sig = inspect.signature(calibrate.run_calibration_offline)
        assert "judge" not in sig.parameters

        calibrate.run_calibration_offline(
            judgments=judgments_file, labels=labels, label_file_hash="fixture-hash", n_resamples=50
        )

    def test_stale_judge_version_raises(self):
        run_a, run_b, labels, judge = _calibration_fixture()
        live_result = run_calibration(
            run_a=run_a,
            run_b=run_b,
            labels=labels,
            judge=judge,
            label_file_hash="fixture-hash",
            n_resamples=50,
            seed=0,
        )
        judgments_file = self._judgments_file_from_live_result(live_result)
        stale = calibrate.JudgmentsFile(
            judge_version="some-old-judge-version-hash",
            written_at=judgments_file.written_at,
            judgments=judgments_file.judgments,
            self_consistency=judgments_file.self_consistency,
        )

        with pytest.raises(calibrate.StaleJudgmentsError, match="Re-run"):
            calibrate.run_calibration_offline(
                judgments=stale, labels=labels, label_file_hash="fixture-hash", n_resamples=50
            )

    def test_output_hash_mismatch_against_label_raises_binding_error(self):
        run_a, run_b, labels, judge = _calibration_fixture()
        live_result = run_calibration(
            run_a=run_a,
            run_b=run_b,
            labels=labels,
            judge=judge,
            label_file_hash="fixture-hash",
            n_resamples=50,
            seed=0,
        )
        judgments_file = self._judgments_file_from_live_result(live_result)
        # Corrupt exactly one persisted judgment's hash -- simulating
        # judgments.jsonl no longer matching labels.jsonl.
        corrupted = list(judgments_file.judgments)
        corrupted[0] = calibrate.JudgmentRecord(
            item_id=corrupted[0].item_id,
            candidate=corrupted[0].candidate,
            field=corrupted[0].field,
            verdict=corrupted[0].verdict,
            error=corrupted[0].error,
            rationale=corrupted[0].rationale,
            output_sha256="0" * 64,
            judge_version=corrupted[0].judge_version,
        )
        corrupted_file = calibrate.JudgmentsFile(
            judge_version=judgments_file.judge_version,
            written_at=judgments_file.written_at,
            judgments=tuple(corrupted),
            self_consistency=judgments_file.self_consistency,
        )

        with pytest.raises(calibrate.CalibrationBindingError):
            calibrate.run_calibration_offline(
                judgments=corrupted_file, labels=labels, label_file_hash="fixture-hash",
                n_resamples=50,
            )

    def test_no_matching_labels_raises(self):
        run_a, run_b, labels, judge = _calibration_fixture()
        live_result = run_calibration(
            run_a=run_a,
            run_b=run_b,
            labels=labels,
            judge=judge,
            label_file_hash="fixture-hash",
            n_resamples=50,
            seed=0,
        )
        judgments_file = self._judgments_file_from_live_result(live_result)

        with pytest.raises(ValueError, match="no labeled judgment"):
            calibrate.run_calibration_offline(
                judgments=judgments_file, labels=[], label_file_hash="fixture-hash",
                n_resamples=50,
            )

    def test_retest_ceiling_computed_purely_from_labels(self):
        run_a, run_b, labels, judge = _calibration_fixture()
        live_result = run_calibration(
            run_a=run_a,
            run_b=run_b,
            labels=labels,
            judge=judge,
            label_file_hash="fixture-hash",
            n_resamples=50,
            seed=0,
        )
        judgments_file = self._judgments_file_from_live_result(live_result)
        # Must use same candidate_value as initial labels (output_sha256 binding).
        def cv(item_id: str, candidate: str, field: str) -> str:
            return f"{item_id}-{candidate}-{field}-value"

        retest_labels = [
            make_label(
                "cal-001",
                "a",
                "issue_summary",
                "pass",
                round_="retest",
                candidate_value=cv("cal-001", "a", "issue_summary"),
            ),
            make_label(
                "cal-002",
                "a",
                "issue_summary",
                "fail",
                round_="retest",
                candidate_value=cv("cal-002", "a", "issue_summary"),
            ),
            make_label(
                "cal-003",
                "a",
                "issue_summary",
                "pass",
                round_="retest",
                candidate_value=cv("cal-003", "a", "issue_summary"),
            ),
        ]

        offline_result = calibrate.run_calibration_offline(
            judgments=judgments_file,
            labels=labels + retest_labels,
            label_file_hash="fixture-hash",
            n_resamples=50,
            retest=True,
        )

        assert offline_result.ceiling is not None
        assert offline_result.ceiling.kappa == pytest.approx(1.0)


# --------------------------------------------------------------------------
# `eval calibrate` CLI wiring (typer.testing.CliRunner). No live API calls:
# candidate runs are pre-seeded via run_eval with fakes, the judge is a
# scripted fake, and TraceContext is replaced with an always-traced fake
# where a test needs `--retest`/happy-path behavior to proceed.
# --------------------------------------------------------------------------

CLI_DEFAULT_CONFIG_PATH = Path(__file__).parents[2] / "configs" / "default.yaml"

cli_runner = CliRunner()


def _cli_item(item_id: str) -> GoldenItem:
    return GoldenItem(
        id=item_id,
        email=EmailInput(**{"from": f"{item_id}@example.com", "subject": item_id, "body": "Body."}),
        expected=GoldenExpected(
            category="billing",
            priority="normal",
            customer_name="Jane Doe",
            order_id=None,
            product_name=None,
            issue_summary=f"{item_id}-ref-issue",
            requested_action=f"{item_id}-ref-action",
        ),
        meta=GoldenMeta(
            slice="nominal",
            categories=["billing"],
            difficulty=1,
            generator="gpt-4",
            edited=False,
            notes="",
        ),
    )


def _write_dataset(path: Path, items: list[GoldenItem]) -> Path:
    lines = [json.dumps(item.model_dump(mode="json")) for item in items]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_labels(path: Path, labels: list[CalibrationLabel]) -> Path:
    lines = [json.dumps(label.model_dump(mode="json")) for label in labels]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_calibrate_config(path: Path, *, k: int = 1) -> Path:
    base = load_config(CLI_DEFAULT_CONFIG_PATH)
    updated = base.model_copy(update={"k": k})
    path.write_text(yaml.safe_dump(updated.model_dump(mode="json")), encoding="utf-8")
    return path


@dataclass
class _CalibrationCandidateClient:
    """Fake candidate client: each item's output embeds the item id (parsed
    from the rendered prompt's "Subject: {subject}" line, where the test's
    items set ``subject = item_id`` exactly) and ``candidate``, so every
    (item, candidate) pair produces a distinguishable candidate_value."""

    candidate: str
    calls: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def complete_structured(self, prompt: str, schema: type[BaseModel]) -> StructuredResult:
        with self._lock:
            self.calls.append(prompt)
        subject_line = next(line for line in prompt.splitlines() if line.startswith("Subject: "))
        item_id = subject_line.removeprefix("Subject: ").strip()
        output = TicketExtraction(
            category="billing",
            priority="normal",
            customer_name="Jane Doe",
            order_id=None,
            product_name=None,
            issue_summary=f"{item_id}-{self.candidate}-issue",
            requested_action=f"{item_id}-{self.candidate}-action",
        )
        return StructuredResult(
            output=output,
            failure=None,
            raw=output.model_dump_json(),
            usage=Usage(input_tokens=10, output_tokens=5),
            served_model_version=f"candidate-{self.candidate}-v1",
        )


@dataclass
class _AlwaysPassJudgeClient:
    """Fake judge client used only for pre-seeding candidate runs via
    run_eval -- its verdicts are never consumed by calibrate.py, which
    re-judges independently through its own, separately-scripted judge."""

    def complete_structured(self, prompt: str, schema: type[BaseModel]) -> StructuredResult:
        output = JudgeVerdict(verdict="pass", rationale="run-time judge, unused by calibrate")
        return StructuredResult(
            output=output,
            failure=None,
            raw=output.model_dump_json(),
            usage=Usage(input_tokens=1, output_tokens=1),
            served_model_version="run-time-judge-v1",
        )


class _FakeTraceContext:
    """Duck-typed stand-in for ``TraceContext`` -- always "traced", never
    touches Langfuse (mirrors test_cli.py's own fake)."""

    untraced = False

    @staticmethod
    def for_run(config: object, reportable: bool, **kwargs: object) -> _FakeTraceContext:
        return _FakeTraceContext()

    def candidate_span(self, **kwargs: object):
        return contextlib.nullcontext()

    def judge_span(self, **kwargs: object):
        return contextlib.nullcontext()

    def record_item_scores(self, **kwargs: object) -> None:
        pass

    def flush(self) -> None:
        pass


def _seed_calibration_runs(effective_cfg, items: list[GoldenItem]) -> None:
    """Pre-seeds completed candidate runs for both labels via ``run_eval``
    directly (never through the CLI) -- mirrors test_cli.py's
    ``TestCompareReuse`` convention so ``eval calibrate`` can be proven to
    reuse them without constructing any real provider client."""

    for label in ("a", "b"):
        run_eval(
            effective_cfg,
            ModelKey(
                label=label,
                candidate_client=_CalibrationCandidateClient(candidate=label),
                judge_client=_AlwaysPassJudgeClient(),
            ),
            k=effective_cfg.k,
            dataset=items,
            prompt=EXTRACTION_PROMPT,
            runs_root=DEFAULT_RUNS_ROOT,
        )


def _happy_path_candidate_value(item_id: str, candidate: str, field: str) -> str:
    """The exact ``candidate_value`` ``_CalibrationCandidateClient`` produces
    for ``(item_id, candidate, field)`` -- ``issue_summary=f"{item_id}-
    {candidate}-issue"``/``requested_action=f"{item_id}-{candidate}-action"``
    -- so ``_happy_path_labels`` can bind (F1) each label's ``output_sha256``
    to the SAME value ``build_triples`` will reconstruct from the seeded run.
    """

    short = "issue" if field == "issue_summary" else "action"
    return f"{item_id}-{candidate}-{short}"


def _happy_path_labels(item_ids: list[str]) -> list[CalibrationLabel]:
    """Owner labels matching ``_CalibrationCandidateClient``'s output exactly
    -- perfect agreement -- with one "fail" per candidate (so neither
    candidate's per-candidate subset collapses to a single category)."""

    fail_pairs = {(item_ids[0], "a", "issue_summary"), (item_ids[1], "b", "requested_action")}
    labels: list[CalibrationLabel] = []
    for item_id in item_ids:
        for candidate in ("a", "b"):
            for f in ("issue_summary", "requested_action"):
                verdict = "fail" if (item_id, candidate, f) in fail_pairs else "pass"
                labels.append(
                    make_label(
                        item_id,
                        candidate,
                        f,
                        verdict,
                        candidate_value=_happy_path_candidate_value(item_id, candidate, f),
                    )
                )
    return labels


def _happy_path_judge_client() -> _KeyedJudgeClient:
    """Scripted judge that agrees exactly with ``_happy_path_labels`` --
    verdict for a candidate_value of the form ``{item}-{candidate}-{field}``
    is "fail" iff it matches one of the deliberately fail-labeled pairs."""

    def verdict_for(candidate_value: str, idx: int) -> str:
        # candidate_value looks like "cal-001-a-issue"/"cal-002-b-action".
        parts = candidate_value.rsplit("-", 2)
        item_id, candidate, short_field = parts[0], parts[1], parts[2]
        field = "issue_summary" if short_field == "issue" else "requested_action"
        fail_pairs = {("cal-001", "a", "issue_summary"), ("cal-002", "b", "requested_action")}
        return "fail" if (item_id, candidate, field) in fail_pairs else "pass"

    return _KeyedJudgeClient(verdict_for=verdict_for)


class TestCalibrateCLIFailFast:
    def test_fails_fast_without_langfuse_keys_before_any_client_construction(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

        emails_path = _write_dataset(tmp_path / "emails.jsonl", [_cli_item("cal-001")])
        labels_path = _write_labels(tmp_path / "labels.jsonl", [])
        config_path = _write_calibrate_config(tmp_path / "config.yaml")

        def _forbid_build_model_key(label, config):
            raise AssertionError("_build_model_key must not be called before the fail-fast check")

        def _forbid_build_judge_client(config):
            raise AssertionError(
                "_build_judge_client must not be called before the fail-fast check"
            )

        monkeypatch.setattr(cli, "_build_model_key", _forbid_build_model_key)
        monkeypatch.setattr(cli, "_build_judge_client", _forbid_build_judge_client)

        result = cli_runner.invoke(
            app,
            [
                "calibrate",
                "--emails",
                str(emails_path),
                "--labels",
                str(labels_path),
                "--config",
                str(config_path),
            ],
        )

        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "credentials" in result.output.lower() or "langfuse" in result.output.lower()


class TestCalibrateCLIHappyPath:
    def test_reuses_existing_runs_writes_certificate_and_prints_report(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        item_ids = ["cal-001", "cal-002", "cal-003"]
        items = [_cli_item(i) for i in item_ids]
        emails_path = _write_dataset(tmp_path / "emails.jsonl", items)
        labels_path = _write_labels(tmp_path / "labels.jsonl", _happy_path_labels(item_ids))
        config_path = _write_calibrate_config(tmp_path / "config.yaml", k=1)

        cfg = load_config(config_path)
        effective_cfg, calib_items = cli._resolve_calibration_dataset(cfg, emails_path)
        _seed_calibration_runs(effective_cfg, calib_items)

        monkeypatch.setattr(cli, "TraceContext", _FakeTraceContext)

        def _forbid_build_model_key(label, config):
            raise AssertionError("_build_model_key must not be called -- both runs are reused")

        monkeypatch.setattr(cli, "_build_model_key", _forbid_build_model_key)
        monkeypatch.setattr(cli, "_build_judge_client", lambda config: _happy_path_judge_client())

        result = cli_runner.invoke(
            app,
            [
                "calibrate",
                "--emails",
                str(emails_path),
                "--labels",
                str(labels_path),
                "--config",
                str(config_path),
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Judge Calibration Report" in result.output

        cert_path = tmp_path / "data" / "calibration" / "certificate.json"
        assert cert_path.exists()
        certificate = Certificate.model_validate(json.loads(cert_path.read_text(encoding="utf-8")))
        assert certificate.verdict == "adequate"
        assert certificate.overall_kappa == pytest.approx(1.0)
        assert certificate.per_candidate_kappa_ci is not None
        assert certificate.label_file_hash == hash_label_file(labels_path)

        # Finding F2: the live run must persist its judge output so a later
        # `--offline` invocation can recompute without any API calls.
        judgments_path = tmp_path / "data" / "calibration" / "judgments.jsonl"
        assert judgments_path.exists()
        assert "Judgments written to" in result.output
        judgments = calibrate.load_judgments_jsonl(judgments_path)
        assert judgments.judge_version == judge_version()
        assert len(judgments.judgments) == len(item_ids) * 2 * 2  # 2 candidates x 2 fields
        assert len(judgments.self_consistency) > 0

    def test_date_option_overrides_certificate_date(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        item_ids = ["cal-001", "cal-002", "cal-003"]
        items = [_cli_item(i) for i in item_ids]
        emails_path = _write_dataset(tmp_path / "emails.jsonl", items)
        labels_path = _write_labels(tmp_path / "labels.jsonl", _happy_path_labels(item_ids))
        config_path = _write_calibrate_config(tmp_path / "config.yaml", k=1)

        cfg = load_config(config_path)
        effective_cfg, calib_items = cli._resolve_calibration_dataset(cfg, emails_path)
        _seed_calibration_runs(effective_cfg, calib_items)

        monkeypatch.setattr(cli, "TraceContext", _FakeTraceContext)
        monkeypatch.setattr(
            cli,
            "_build_model_key",
            lambda label, config: (_ for _ in ()).throw(AssertionError("must reuse")),
        )
        monkeypatch.setattr(cli, "_build_judge_client", lambda config: _happy_path_judge_client())

        result = cli_runner.invoke(
            app,
            [
                "calibrate",
                "--emails",
                str(emails_path),
                "--labels",
                str(labels_path),
                "--config",
                str(config_path),
                "--date",
                "2030-01-01",
            ],
        )

        assert result.exit_code == 0, result.output
        cert_path = tmp_path / "data" / "calibration" / "certificate.json"
        certificate = Certificate.model_validate(json.loads(cert_path.read_text(encoding="utf-8")))
        assert certificate.date == date(2030, 1, 1)

    def test_retest_flag_adds_ceiling_to_certificate(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        item_ids = ["cal-001", "cal-002", "cal-003"]
        items = [_cli_item(i) for i in item_ids]
        emails_path = _write_dataset(tmp_path / "emails.jsonl", items)
        labels = _happy_path_labels(item_ids)
        # Retest labels must use same candidate_value as initial labels (output_sha256 binding).
        retest_labels = [
            make_label(
                "cal-001",
                "a",
                "issue_summary",
                "fail",
                round_="retest",
                candidate_value=_happy_path_candidate_value("cal-001", "a", "issue_summary"),
            ),
            make_label(
                "cal-002",
                "a",
                "issue_summary",
                "pass",
                round_="retest",
                candidate_value=_happy_path_candidate_value("cal-002", "a", "issue_summary"),
            ),
            make_label(
                "cal-003",
                "a",
                "issue_summary",
                "pass",
                round_="retest",
                candidate_value=_happy_path_candidate_value("cal-003", "a", "issue_summary"),
            ),
        ]
        labels_path = _write_labels(tmp_path / "labels.jsonl", labels + retest_labels)
        config_path = _write_calibrate_config(tmp_path / "config.yaml", k=1)

        cfg = load_config(config_path)
        effective_cfg, calib_items = cli._resolve_calibration_dataset(cfg, emails_path)
        _seed_calibration_runs(effective_cfg, calib_items)

        monkeypatch.setattr(cli, "TraceContext", _FakeTraceContext)
        monkeypatch.setattr(
            cli,
            "_build_model_key",
            lambda label, config: (_ for _ in ()).throw(AssertionError("must reuse")),
        )
        monkeypatch.setattr(cli, "_build_judge_client", lambda config: _happy_path_judge_client())

        result = cli_runner.invoke(
            app,
            [
                "calibrate",
                "--retest",
                "--emails",
                str(emails_path),
                "--labels",
                str(labels_path),
                "--config",
                str(config_path),
            ],
        )

        assert result.exit_code == 0, result.output
        assert "an estimate of the consistency ceiling" in result.output
        cert_path = tmp_path / "data" / "calibration" / "certificate.json"
        certificate = Certificate.model_validate(json.loads(cert_path.read_text(encoding="utf-8")))
        assert certificate.ceiling_kappa is not None


# --------------------------------------------------------------------------
# `eval calibrate` forces K=1 regardless of config.k (finding F3)
# --------------------------------------------------------------------------


class TestCalibrateForcesK1:
    def test_calibrate_runs_candidates_at_k1_regardless_of_config_k(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        item_ids = ["cal-001", "cal-002", "cal-003"]
        items = [_cli_item(i) for i in item_ids]
        emails_path = _write_dataset(tmp_path / "emails.jsonl", items)
        labels_path = _write_labels(tmp_path / "labels.jsonl", _happy_path_labels(item_ids))
        # config.k = 3 -- calibrate must still run each candidate at k=1 (F3),
        # never inheriting this. No pre-seeded runs here on purpose: this
        # test needs _build_model_key to actually fire so it can observe how
        # many candidate calls a fresh run makes.
        config_path = _write_calibrate_config(tmp_path / "config.yaml", k=3)

        monkeypatch.setattr(cli, "TraceContext", _FakeTraceContext)

        registry: dict[str, list[_CalibrationCandidateClient]] = {}

        def factory(label: str, config: object) -> ModelKey:
            candidate_client = _CalibrationCandidateClient(candidate=label)
            registry.setdefault(label, []).append(candidate_client)
            return ModelKey(
                label=label,
                candidate_client=candidate_client,
                judge_client=_AlwaysPassJudgeClient(),
            )

        monkeypatch.setattr(cli, "_build_model_key", factory)
        monkeypatch.setattr(cli, "_build_judge_client", lambda config: _happy_path_judge_client())

        result = cli_runner.invoke(
            app,
            [
                "calibrate",
                "--emails",
                str(emails_path),
                "--labels",
                str(labels_path),
                "--config",
                str(config_path),
            ],
        )

        assert result.exit_code == 0, result.output
        for label in ("a", "b"):
            assert len(registry[label]) == 1  # _build_model_key called exactly once per candidate
            calls = registry[label][0].calls
            # Exactly one candidate call per item -- NOT k=3 x len(item_ids).
            assert len(calls) == len(item_ids)


# --------------------------------------------------------------------------
# `eval calibrate --offline` (finding F2): zero client construction, no
# tracing requirement, and the loud-failure contracts (stale judge_version,
# output_sha256 mismatch).
# --------------------------------------------------------------------------


class _RaisingProviderClient:
    """Stand-in for AnthropicClient/OpenAIClient/GeminiClient that raises on
    construction -- proves --offline never reaches real provider SDK client
    construction (mirrors test_cli.py's ``TestRescore`` pattern, finding F2)."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise AssertionError(
            f"{self.__class__.__name__} must not be constructed in --offline mode"
        )


def _forbid_real_provider_client_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "AnthropicClient", _RaisingProviderClient)
    monkeypatch.setattr(cli, "OpenAIClient", _RaisingProviderClient)
    monkeypatch.setattr(cli, "GeminiClient", _RaisingProviderClient)


def _seed_live_judgments(tmp_path: Path) -> tuple[Path, Path]:
    """Runs `eval calibrate` live once through the CLI (candidate runs pre-
    seeded/faked via _build_model_key, judge faked via _build_judge_client,
    TraceContext faked) to produce a real, on-disk judgments.jsonl +
    certificate.json this module's ``--offline`` tests can then consume.
    Returns ``(labels_path, judgments_path)``."""

    item_ids = ["cal-001", "cal-002", "cal-003"]
    items = [_cli_item(i) for i in item_ids]
    emails_path = _write_dataset(tmp_path / "emails.jsonl", items)
    labels_path = _write_labels(tmp_path / "labels.jsonl", _happy_path_labels(item_ids))
    config_path = _write_calibrate_config(tmp_path / "config.yaml", k=1)

    cfg = load_config(config_path)
    effective_cfg, calib_items = cli._resolve_calibration_dataset(cfg, emails_path)
    _seed_calibration_runs(effective_cfg, calib_items)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "TraceContext", _FakeTraceContext)
        mp.setattr(
            cli,
            "_build_model_key",
            lambda label, config: (_ for _ in ()).throw(AssertionError("must reuse")),
        )
        mp.setattr(cli, "_build_judge_client", lambda config: _happy_path_judge_client())
        result = cli_runner.invoke(
            app,
            [
                "calibrate",
                "--emails",
                str(emails_path),
                "--labels",
                str(labels_path),
                "--config",
                str(config_path),
            ],
        )
    assert result.exit_code == 0, result.output

    judgments_path = tmp_path / "data" / "calibration" / "judgments.jsonl"
    return labels_path, judgments_path


class TestCalibrateCLIOffline:
    def test_offline_recompute_matches_live_certificate_with_zero_client_construction(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        labels_path, judgments_path = _seed_live_judgments(tmp_path)
        cert_path = tmp_path / "data" / "calibration" / "certificate.json"
        live_certificate = Certificate.model_validate(
            json.loads(cert_path.read_text(encoding="utf-8"))
        )

        def _forbid(*args: object, **kwargs: object) -> None:
            raise AssertionError("must not be called in --offline mode")

        monkeypatch.setattr(cli, "_build_model_key", _forbid)
        monkeypatch.setattr(cli, "_build_judge_client", _forbid)
        _forbid_real_provider_client_construction(monkeypatch)

        result = cli_runner.invoke(
            app,
            [
                "calibrate",
                "--offline",
                "--labels",
                str(labels_path),
                "--judgments",
                str(judgments_path),
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Judge Calibration Report" in result.output
        offline_certificate = Certificate.model_validate(
            json.loads(cert_path.read_text(encoding="utf-8"))
        )
        assert offline_certificate.overall_kappa == pytest.approx(live_certificate.overall_kappa)
        assert offline_certificate.kappa_ci == pytest.approx(live_certificate.kappa_ci)
        assert offline_certificate.verdict == live_certificate.verdict

    def test_offline_requires_no_langfuse_credentials_and_never_touches_trace_context(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        labels_path, judgments_path = _seed_live_judgments(tmp_path)
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

        def _forbid_for_run(*args: object, **kwargs: object) -> None:
            raise AssertionError("TraceContext must never be constructed in --offline mode")

        monkeypatch.setattr(cli.TraceContext, "for_run", staticmethod(_forbid_for_run))

        result = cli_runner.invoke(
            app,
            [
                "calibrate",
                "--offline",
                "--labels",
                str(labels_path),
                "--judgments",
                str(judgments_path),
            ],
        )

        assert result.exit_code == 0, result.output

    def test_stale_judge_version_gives_clean_exit_one(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        labels_path, judgments_path = _seed_live_judgments(tmp_path)
        judgments = calibrate.load_judgments_jsonl(judgments_path)
        calibrate.write_judgments_jsonl(
            judgments_path,
            judgments=judgments.judgments,
            self_consistency=judgments.self_consistency,
            judge_version="stale-judge-version-hash",
        )

        result = cli_runner.invoke(
            app,
            [
                "calibrate",
                "--offline",
                "--labels",
                str(labels_path),
                "--judgments",
                str(judgments_path),
            ],
        )

        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "judge_version" in result.output.lower() or "stale" in result.output.lower()

    def test_output_hash_mismatch_gives_clean_exit_one(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        labels_path, judgments_path = _seed_live_judgments(tmp_path)
        judgments = calibrate.load_judgments_jsonl(judgments_path)
        first = judgments.judgments[0]
        corrupted_first = calibrate.JudgmentRecord(
            item_id=first.item_id,
            candidate=first.candidate,
            field=first.field,
            verdict=first.verdict,
            error=first.error,
            rationale=first.rationale,
            output_sha256="0" * 64,
            judge_version=first.judge_version,
        )
        calibrate.write_judgments_jsonl(
            judgments_path,
            judgments=[corrupted_first, *judgments.judgments[1:]],
            self_consistency=judgments.self_consistency,
            judge_version=judgments.judge_version,
        )

        result = cli_runner.invoke(
            app,
            [
                "calibrate",
                "--offline",
                "--labels",
                str(labels_path),
                "--judgments",
                str(judgments_path),
            ],
        )

        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert first.item_id in result.output
