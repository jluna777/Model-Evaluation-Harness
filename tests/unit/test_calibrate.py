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
    GoldLabel,
    JudgedTriple,
    PairedJudgment,
    SelfConsistencyResult,
    Triple,
    build_certificate,
    build_triples,
    check_disjoint_from_golden,
    compute_agreement,
    compute_iaa_ceiling,
    decide_verdict,
    hash_label_file,
    judge_triples,
    load_calibration_labels,
    measure_self_consistency,
    pair_with_labels,
    per_candidate_divergence_flag,
    render_calibration_report,
    resolve_certificate_date,
    resolve_gold_labels,
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
    PerturbationOverlay,
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
    annotator: str = "owner",
    label_date_: str = "2026-06-01",
    label_id: str | None = None,
    candidate_value: str = "unused-value",
) -> CalibrationLabel:
    """``candidate_value`` (finding F1) defaults to an arbitrary placeholder
    for tests that never pair this label against a real judged triple (date/
    hash-file/round-trip tests, and adjudication-round labels, which are only
    ever compared to the two annotators' rows, never directly to a candidate
    output). Any test that DOES flow through ``pair_with_labels``/
    ``run_calibration`` for its ``"initial"`` round labels must pass the SAME
    ``candidate_value`` string the corresponding ``Triple``/candidate output
    actually carries, or the F1 binding check will (correctly) raise
    ``CalibrationBindingError``.

    ``annotator`` defaults to ``"owner"`` (the primary annotator, dual-
    annotation upgrade 2026-07-09) -- pass e.g. ``annotator="annotator2"``
    for the second annotator's rows."""

    return CalibrationLabel(
        label_id=label_id or f"lbl-{item_id}-{candidate}-{field}-{round_}-{annotator}",
        item_id=item_id,
        candidate=candidate,
        field=field,
        annotator=annotator,
        verdict=verdict,
        critique="scripted",
        label_date=label_date_,
        round=round_,
        output_sha256=calibrate.hash_output(candidate_value),
    )


def make_gold(
    item_id: str,
    candidate: str,
    field: str,
    verdict: str,
    *,
    critique: str = "scripted",
    candidate_value: str = "unused-value",
    source: str = "agreement",
) -> GoldLabel:
    """A ``GoldLabel`` built directly (bypassing ``resolve_gold_labels``) for
    tests exercising ``pair_with_labels``/``pair_judgments_with_labels`` in
    isolation from gold resolution itself."""

    return GoldLabel(
        item_id=item_id,
        candidate=candidate,
        field=field,
        verdict=verdict,
        critique=critique,
        output_sha256=calibrate.hash_output(candidate_value),
        source=source,
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
    def test_pairs_matching_gold_and_excludes_judge_errors(self):
        email = make_item("cal-001").email
        triples = [
            Triple("cal-001", "a", "issue_summary", email, "ref", "v1"),  # gold, pass/pass
            Triple("cal-001", "a", "requested_action", email, "ref", "v2"),  # judge error
        ]
        judged = [
            JudgedTriple(triples[0], verdict="pass", error=None, rationale="ok"),
            JudgedTriple(triples[1], verdict=None, error="refusal", rationale=None),
        ]
        gold = [
            make_gold("cal-001", "a", "issue_summary", "pass", candidate_value="v1"),
            make_gold("cal-001", "a", "requested_action", "pass", candidate_value="v2"),
        ]

        paired, judge_errors, valid_keys = pair_with_labels(judged, gold)

        assert len(paired) == 1
        assert paired[0].item_id == "cal-001"
        assert paired[0].owner_verdict == "pass"
        assert paired[0].judge_verdict == "pass"
        assert judge_errors == 1
        assert valid_keys == (("cal-001", "a", "issue_summary"),)

    def test_duplicate_gold_for_same_key_raises(self):
        gold = [
            make_gold("cal-001", "a", "issue_summary", "pass"),
            make_gold("cal-001", "a", "issue_summary", "fail"),
        ]

        with pytest.raises(ValueError, match="duplicate"):
            pair_with_labels([], gold)


# --------------------------------------------------------------------------
# pair_with_labels: population-parity invariant (owner-ruled, 2026-07-09)
# --------------------------------------------------------------------------


class TestPairWithLabelsPopulationParity:
    def test_gold_without_judgment_raises(self):
        """Case 1: a gold label whose key has no corresponding judged entry
        at all raises DualAnnotationError, not a silent skip."""

        gold = [make_gold("cal-999", "a", "issue_summary", "pass", candidate_value="v")]

        with pytest.raises(calibrate.DualAnnotationError) as excinfo:
            pair_with_labels([], gold)

        message = str(excinfo.value)
        assert "no corresponding judgment" in message
        assert "('cal-999', 'a', 'issue_summary')" in message

    def test_judgment_unlabeled_by_both_annotators_raises(self):
        """Case 2: a judged triple whose key was labeled by neither
        annotator (absent from gold) raises DualAnnotationError -- replaces
        the old unlabeled_excluded tolerance."""

        email = make_item("cal-001").email
        triple = Triple("cal-001", "a", "issue_summary", email, "ref", "v1")
        judged = [JudgedTriple(triple, verdict="pass", error=None, rationale="ok")]

        with pytest.raises(calibrate.DualAnnotationError) as excinfo:
            pair_with_labels(judged, [])

        message = str(excinfo.value)
        assert "labeled by neither annotator" in message
        assert "('cal-001', 'a', 'issue_summary')" in message

    def test_both_directions_reported_together(self):
        """Both population-parity violations, when present at once, are
        reported in a single error, mirroring the existing all-violations-
        together convention (`_validate_adjudication_round`)."""

        email = make_item("cal-001").email
        triple = Triple("cal-001", "a", "issue_summary", email, "ref", "v1")
        judged = [JudgedTriple(triple, verdict="pass", error=None, rationale="ok")]
        gold = [make_gold("cal-999", "a", "issue_summary", "pass", candidate_value="v")]

        with pytest.raises(calibrate.DualAnnotationError) as excinfo:
            pair_with_labels(judged, gold)

        message = str(excinfo.value)
        assert "no corresponding judgment" in message
        assert "labeled by neither annotator" in message

    def test_judge_error_key_is_tolerated_and_excluded_from_valid_keys(self):
        """The ONE tolerated gap: a judge error on a key that IS doubly
        labeled excludes it from `paired` and from the returned `valid_keys`,
        without raising."""

        email = make_item("cal-001").email
        triples = [
            Triple("cal-001", "a", "issue_summary", email, "ref", "v1"),
            Triple("cal-002", "a", "issue_summary", email, "ref", "v2"),
        ]
        judged = [
            JudgedTriple(triples[0], verdict=None, error="refusal", rationale=None),
            JudgedTriple(triples[1], verdict="pass", error=None, rationale="ok"),
        ]
        gold = [
            make_gold("cal-001", "a", "issue_summary", "pass", candidate_value="v1"),
            make_gold("cal-002", "a", "issue_summary", "pass", candidate_value="v2"),
        ]

        paired, judge_errors, valid_keys = pair_with_labels(judged, gold)

        assert judge_errors == 1
        assert len(paired) == 1
        assert valid_keys == (("cal-002", "a", "issue_summary"),)


# --------------------------------------------------------------------------
# pair_with_labels: output-binding check (finding F1)
# --------------------------------------------------------------------------


class TestPairWithLabelsOutputBinding:
    def test_hash_mismatch_raises_calibration_binding_error_naming_the_key(self):
        email = make_item("cal-001").email
        triple = Triple("cal-001", "a", "issue_summary", email, "ref", "v1")
        judged = [JudgedTriple(triple, verdict="pass", error=None, rationale="ok")]
        # Gold label was resolved against a DIFFERENT candidate_value than the
        # one actually judged now -- e.g. the run directory was regenerated.
        gold = [make_gold("cal-001", "a", "issue_summary", "pass", candidate_value="stale-v1")]

        with pytest.raises(calibrate.CalibrationBindingError) as excinfo:
            pair_with_labels(judged, gold)

        assert excinfo.value.mismatches == (("cal-001", "a", "issue_summary"),)
        assert "cal-001" in str(excinfo.value)

    def test_one_mismatch_blocks_all_pairing_not_just_the_bad_key(self):
        """All-or-nothing (F1): a single mismatched gold label must prevent
        EVERY pair from being returned, including otherwise-correctly-bound
        ones."""

        email = make_item("cal-001").email
        good_triple = Triple("cal-001", "a", "issue_summary", email, "ref", "good-value")
        bad_triple = Triple("cal-002", "a", "issue_summary", email, "ref", "good-value-2")
        judged = [
            JudgedTriple(good_triple, verdict="pass", error=None, rationale="ok"),
            JudgedTriple(bad_triple, verdict="pass", error=None, rationale="ok"),
        ]
        gold = [
            make_gold("cal-001", "a", "issue_summary", "pass", candidate_value="good-value"),
            make_gold(
                "cal-002", "a", "issue_summary", "pass", candidate_value="wrong-recorded-value"
            ),
        ]

        with pytest.raises(calibrate.CalibrationBindingError) as excinfo:
            pair_with_labels(judged, gold)

        assert excinfo.value.mismatches == (("cal-002", "a", "issue_summary"),)

    def test_matching_hash_pairs_normally(self):
        email = make_item("cal-001").email
        triple = Triple("cal-001", "a", "issue_summary", email, "ref", "exact-value")
        judged = [JudgedTriple(triple, verdict="pass", error=None, rationale="ok")]
        gold = [make_gold("cal-001", "a", "issue_summary", "pass", candidate_value="exact-value")]

        paired, judge_errors, valid_keys = pair_with_labels(judged, gold)

        assert len(paired) == 1
        assert judge_errors == 0
        assert valid_keys == (("cal-001", "a", "issue_summary"),)

    def test_unlabeled_triple_is_never_hash_checked_but_raises_population_parity_error(self):
        """A triple with no matching gold label at all can't mismatch by
        hash -- there is no label to compare against -- but under the
        population-parity invariant (2026-07-09) it is no longer silently
        counted "unlabeled": it raises DualAnnotationError instead."""

        email = make_item("cal-001").email
        triple = Triple("cal-001", "a", "issue_summary", email, "ref", "anything")
        judged = [JudgedTriple(triple, verdict="pass", error=None, rationale="ok")]

        with pytest.raises(calibrate.DualAnnotationError, match="labeled by neither annotator"):
            pair_with_labels(judged, [])


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

        rows = calibrate.labeling_template_rows(triples, "owner")

        assert len(rows) == 2
        assert rows[0] == {
            "item_id": "cal-001",
            "candidate": "a",
            "field": "issue_summary",
            "annotator": "owner",
            "candidate_value": "candidate-value-1",
            "output_sha256": calibrate.hash_output("candidate-value-1"),
            "verdict": "",
            "critique": "",
        }
        assert rows[1]["output_sha256"] == calibrate.hash_output("candidate-value-2")

    def test_empty_triples_gives_empty_rows(self):
        assert calibrate.labeling_template_rows([], "owner") == []

    def test_rows_are_born_correctly_bound_for_calibration_label(self):
        """Every row's output_sha256 must equal hash_output(candidate_value)
        exactly, since a future generator will feed candidate_value/
        output_sha256 straight into a CalibrationLabel once verdict/critique
        are filled in by hand."""

        email = make_item("cal-001").email
        triples = [Triple("cal-001", "b", "issue_summary", email, "ref", "some output text")]

        rows = calibrate.labeling_template_rows(triples, "annotator2")

        assert rows[0]["output_sha256"] == calibrate.hash_output(rows[0]["candidate_value"])

    def test_one_sheet_per_annotator_same_triples_differ_only_by_annotator(self):
        """Dual-annotation upgrade (2026-07-09): calling this once per
        annotator with the SAME triples produces sheets that are identical
        except for the ``annotator`` field -- neither sheet leaks the other's
        (blank, at generation time) verdict column."""

        email = make_item("cal-001").email
        triples = [Triple("cal-001", "a", "issue_summary", email, "ref", "shared-value")]

        owner_rows = calibrate.labeling_template_rows(triples, "owner")
        other_rows = calibrate.labeling_template_rows(triples, "annotator2")

        assert owner_rows[0]["annotator"] == "owner"
        assert other_rows[0]["annotator"] == "annotator2"
        owner_row_sans_annotator = {k: v for k, v in owner_rows[0].items() if k != "annotator"}
        other_row_sans_annotator = {k: v for k, v in other_rows[0].items() if k != "annotator"}
        assert owner_row_sans_annotator == other_row_sans_annotator


# --------------------------------------------------------------------------
# Perturbation overlay (fail-probe design, D2 amendment 2026-07-10):
# validate_perturbation_overlay / apply_perturbation_overlay /
# load_perturbation_overlay.
# --------------------------------------------------------------------------


def make_overlay_row(
    item_id: str,
    candidate: str,
    field: str,
    perturbed_value: str,
    *,
    corruption_type: str = "ungrounded_addition",
    rationale: str = "scripted probe",
) -> PerturbationOverlay:
    return PerturbationOverlay(
        item_id=item_id,
        candidate=candidate,
        field=field,
        perturbed_value=perturbed_value,
        corruption_type=corruption_type,
        rationale=rationale,
    )


class TestValidatePerturbationOverlay:
    def test_valid_overlay_is_accepted_and_returned_by_key(self):
        overlay = [make_overlay_row("cal-101", "a", "issue_summary", "corrupted text")]

        validated = calibrate.validate_perturbation_overlay(
            overlay,
            probe_item_ids={"cal-101", "cal-102"},
            valid_probe_keys={
                ("cal-101", "a", "issue_summary"),
                ("cal-102", "b", "requested_action"),
            },
        )

        assert validated == {("cal-101", "a", "issue_summary"): overlay[0]}

    def test_empty_overlay_returns_empty_mapping(self):
        validated = calibrate.validate_perturbation_overlay(
            [], probe_item_ids={"cal-101"}, valid_probe_keys={("cal-101", "a", "issue_summary")}
        )

        assert validated == {}

    def test_key_targeting_original_emails_file_raises(self):
        """A key whose item_id is NOT among the probe items (e.g. it belongs
        to the original emails.jsonl) must raise, naming the key."""

        overlay = [make_overlay_row("cal-001", "a", "issue_summary", "corrupted text")]

        with pytest.raises(calibrate.PerturbationOverlayError) as excinfo:
            calibrate.validate_perturbation_overlay(
                overlay,
                probe_item_ids={"cal-101"},
                valid_probe_keys={("cal-101", "a", "issue_summary")},
            )

        message = str(excinfo.value)
        assert "target the original emails file" in message
        assert "('cal-001', 'a', 'issue_summary')" in message

    def test_nonexistent_key_raises(self):
        """A probe item_id whose (candidate, field) doesn't correspond to any
        actually-judgeable triple must raise as a nonexistent key -- distinct
        from targeting the original file."""

        overlay = [make_overlay_row("cal-101", "a", "requested_action", "corrupted text")]

        with pytest.raises(calibrate.PerturbationOverlayError) as excinfo:
            calibrate.validate_perturbation_overlay(
                overlay,
                probe_item_ids={"cal-101"},
                valid_probe_keys={("cal-101", "a", "issue_summary")},  # different field
            )

        message = str(excinfo.value)
        assert "don't exist among the fail-probe run's judgeable triples" in message
        assert "('cal-101', 'a', 'requested_action')" in message

    def test_duplicate_key_raises(self):
        overlay = [
            make_overlay_row("cal-101", "a", "issue_summary", "first corrupted text"),
            make_overlay_row("cal-101", "a", "issue_summary", "second corrupted text"),
        ]

        with pytest.raises(calibrate.PerturbationOverlayError) as excinfo:
            calibrate.validate_perturbation_overlay(
                overlay,
                probe_item_ids={"cal-101"},
                valid_probe_keys={("cal-101", "a", "issue_summary")},
            )

        message = str(excinfo.value)
        assert "duplicate overlay row" in message
        assert "('cal-101', 'a', 'issue_summary')" in message

    def test_all_three_violations_reported_together(self):
        overlay = [
            make_overlay_row("cal-001", "a", "issue_summary", "x"),  # targets original file
            make_overlay_row("cal-101", "b", "requested_action", "y"),  # nonexistent
            make_overlay_row("cal-101", "a", "issue_summary", "z1"),
            make_overlay_row("cal-101", "a", "issue_summary", "z2"),  # duplicate of above
        ]

        with pytest.raises(calibrate.PerturbationOverlayError) as excinfo:
            calibrate.validate_perturbation_overlay(
                overlay,
                probe_item_ids={"cal-101"},
                valid_probe_keys={("cal-101", "a", "issue_summary")},
            )

        message = str(excinfo.value)
        assert "target the original emails file" in message
        assert "don't exist among the fail-probe run's judgeable triples" in message
        assert "duplicate overlay row" in message


class TestApplyPerturbationOverlay:
    def test_overlaid_triple_gets_replaced_value(self):
        email = make_item("cal-101").email
        triple = Triple("cal-101", "a", "issue_summary", email, "ref", "real candidate output")
        overlay_row = make_overlay_row("cal-101", "a", "issue_summary", "corrupted output")
        overlay_by_key = {("cal-101", "a", "issue_summary"): overlay_row}

        result = calibrate.apply_perturbation_overlay([triple], overlay_by_key)

        assert len(result) == 1
        assert result[0].candidate_value == "corrupted output"
        # Every other field is preserved unchanged.
        assert result[0].item_id == "cal-101"
        assert result[0].candidate == "a"
        assert result[0].field == "issue_summary"
        assert result[0].email == email
        assert result[0].reference == "ref"

    def test_non_overlaid_triple_keeps_real_output(self):
        email = make_item("cal-101").email
        triple = Triple("cal-101", "a", "issue_summary", email, "ref", "real candidate output")

        result = calibrate.apply_perturbation_overlay([triple], {})

        assert result == [triple]

    def test_mixed_triples_only_matching_keys_replaced(self):
        email = make_item("cal-101").email
        triples = [
            Triple("cal-101", "a", "issue_summary", email, "ref", "real-1"),
            Triple("cal-101", "a", "requested_action", email, "ref", "real-2"),
        ]
        overlay_by_key = {
            ("cal-101", "a", "issue_summary"): make_overlay_row(
                "cal-101", "a", "issue_summary", "corrupted-1"
            )
        }

        result = calibrate.apply_perturbation_overlay(triples, overlay_by_key)

        by_field = {t.field: t for t in result}
        assert by_field["issue_summary"].candidate_value == "corrupted-1"
        assert by_field["requested_action"].candidate_value == "real-2"

    def test_overlaid_value_flows_into_labeling_sheet_and_its_hash_binding(self):
        """The overlay must be applied BEFORE labeling_template_rows is
        called on the reconstructed triples, so a labeling sheet generated
        from a fail-probe run is hash-bound to the CORRUPTED text, never the
        real candidate output -- the design's "everywhere downstream" claim,
        exercised concretely for the labeling-sheet consumer."""

        email = make_item("cal-101").email
        triple = Triple("cal-101", "a", "issue_summary", email, "ref", "real candidate output")
        overlay_row = make_overlay_row("cal-101", "a", "issue_summary", "corrupted output")
        overlaid = calibrate.apply_perturbation_overlay(
            [triple], {("cal-101", "a", "issue_summary"): overlay_row}
        )

        rows = calibrate.labeling_template_rows(overlaid, "owner")

        assert rows[0]["candidate_value"] == "corrupted output"
        assert rows[0]["output_sha256"] == calibrate.hash_output("corrupted output")
        assert rows[0]["output_sha256"] != calibrate.hash_output("real candidate output")


class TestLoadPerturbationOverlay:
    def test_loads_rows_from_file(self, tmp_path):
        path = tmp_path / "perturbations.jsonl"
        row = make_overlay_row("cal-101", "a", "issue_summary", "corrupted text")
        path.write_text(json.dumps(row.model_dump(mode="json")) + "\n", encoding="utf-8")

        rows = calibrate.load_perturbation_overlay(path)

        assert len(rows) == 1
        assert isinstance(rows[0], PerturbationOverlay)
        assert rows[0].item_id == "cal-101"

    def test_absent_file_returns_empty_list(self, tmp_path):
        path = tmp_path / "does-not-exist.jsonl"

        assert calibrate.load_perturbation_overlay(path) == []

    def test_skips_blank_lines(self, tmp_path):
        path = tmp_path / "perturbations.jsonl"
        row = make_overlay_row("cal-101", "a", "issue_summary", "corrupted text")
        path.write_text(f"\n{json.dumps(row.model_dump(mode='json'))}\n\n", encoding="utf-8")

        rows = calibrate.load_perturbation_overlay(path)

        assert len(rows) == 1


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
# compute_real_only_kappa (fail-probe design, D2 amendment 2026-07-10)
# --------------------------------------------------------------------------


class TestComputeRealOnlyKappa:
    def _paired(self, item_id: str, candidate: str, owner: str, judge: str) -> PairedJudgment:
        return PairedJudgment(item_id, candidate, owner, judge)

    def test_restricts_to_non_probe_items(self, monkeypatch):
        captured: list[list[str]] = []
        real_cohens_kappa = agreement_module.cohens_kappa

        def _spy(a, b, *, clusters=None, **kwargs):
            captured.append(list(clusters))
            return real_cohens_kappa(a, b, clusters=clusters, **kwargs)

        monkeypatch.setattr(calibrate, "cohens_kappa", _spy)

        paired = [
            self._paired("cal-001", "a", "pass", "pass"),
            self._paired("cal-002", "a", "fail", "fail"),
            self._paired("cal-003", "a", "pass", "pass"),
            self._paired("cal-101", "a", "fail", "fail"),  # probe item
        ]

        result = calibrate.compute_real_only_kappa(
            paired, probe_item_ids={"cal-101"}, n_resamples=50, seed=0
        )

        assert result is not None
        kappa_result, _warnings = result
        assert kappa_result.kappa == pytest.approx(1.0)
        assert len(captured[0]) == 3  # cal-101 excluded

    def test_empty_probe_ids_uses_full_population(self):
        # >=6 items to avoid jackknife degeneracy in the BCa CI (mirrors
        # TestComputeAgreement's own fixture sizing).
        paired = [self._paired(f"cal-{i:03d}", "a", "pass", "pass") for i in range(1, 6)]
        paired.append(self._paired("cal-006", "a", "fail", "fail"))

        result = calibrate.compute_real_only_kappa(paired, probe_item_ids=set(), n_resamples=50)

        assert result is not None
        kappa_result, _warnings = result
        assert kappa_result.kappa == pytest.approx(1.0)

    def test_returns_none_when_fewer_than_two_non_probe_paired(self):
        paired = [
            self._paired("cal-101", "a", "pass", "pass"),  # probe
            self._paired("cal-102", "a", "fail", "fail"),  # probe
            self._paired("cal-001", "a", "pass", "pass"),  # only one real item
        ]

        result = calibrate.compute_real_only_kappa(
            paired, probe_item_ids={"cal-101", "cal-102"}, n_resamples=50
        )

        assert result is None

    def test_returns_none_when_zero_non_probe_paired(self):
        paired = [self._paired("cal-101", "a", "pass", "pass")]

        result = calibrate.compute_real_only_kappa(paired, probe_item_ids={"cal-101"})

        assert result is None


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
# Human-human agreement (IAA) ceiling -- dual-annotation upgrade, 2026-07-09.
# --------------------------------------------------------------------------


def _dual_labels(
    verdict_pairs: dict[str, tuple[str, str]], *, field: str = "issue_summary"
) -> list[CalibrationLabel]:
    """One owner + one ``annotator2`` label per ``item_id`` in
    ``verdict_pairs`` (``{item_id: (owner_verdict, other_verdict)}``), all
    sharing the same (per-item) ``candidate_value`` so the two annotators'
    ``output_sha256`` match (a genuine disagreement is a verdict difference,
    never a binding mismatch, unless a test deliberately wants the latter)."""

    labels: list[CalibrationLabel] = []
    for item_id, (owner_verdict, other_verdict) in verdict_pairs.items():
        value = f"{item_id}-value"
        labels.append(
            make_label(item_id, "a", field, owner_verdict, annotator="owner", candidate_value=value)
        )
        labels.append(
            make_label(
                item_id, "a", field, other_verdict, annotator="annotator2", candidate_value=value
            )
        )
    return labels


class TestComputeIaaCeiling:
    def test_perfect_agreement_gives_kappa_one(self):
        labels = _dual_labels(
            {
                f"cal-{i:03d}": ("fail" if i == 2 else "pass", "fail" if i == 2 else "pass")
                for i in range(1, 7)
            }
        )

        ceiling, warnings_out = compute_iaa_ceiling(labels, n_resamples=200, seed=0)

        assert ceiling.kappa == pytest.approx(1.0)
        assert isinstance(warnings_out, tuple)

    def test_hand_computed_kappa_on_engineered_disagreement_pattern(self):
        """Owner: pass, pass, fail, fail, pass. Other: pass, fail, fail,
        pass, pass. Hand-computed: n=5, po=3/5=0.6 (3 agreements); row sums
        (owner) fail=2/pass=3, col sums (other) fail=2/pass=3 ->
        pe=(2/5)^2+(3/5)^2=0.52; kappa=(0.6-0.52)/(1-0.52)=0.08/0.48=1/6."""

        labels = _dual_labels(
            {
                "cal-001": ("pass", "pass"),
                "cal-002": ("pass", "fail"),
                "cal-003": ("fail", "fail"),
                "cal-004": ("fail", "pass"),
                "cal-005": ("pass", "pass"),
            }
        )

        ceiling, _ = compute_iaa_ceiling(labels, n_resamples=50, seed=0)

        assert ceiling.kappa == pytest.approx(1 / 6)

    def test_incomplete_second_annotator_coverage_raises_loud_error(self):
        labels = _dual_labels({"cal-001": ("pass", "pass"), "cal-002": ("pass", "pass")})
        # Drop annotator2's row for cal-002 -- incomplete coverage.
        labels = [
            label
            for label in labels
            if not (label.annotator == "annotator2" and label.item_id == "cal-002")
        ]

        with pytest.raises(
            calibrate.DualAnnotationError, match="second annotator labels incomplete"
        ):
            compute_iaa_ceiling(labels)

    def test_cross_annotator_hash_mismatch_raises_binding_error(self):
        labels = [
            make_label(
                "cal-001", "a", "issue_summary", "pass", annotator="owner", candidate_value="v1"
            ),
            make_label(
                "cal-001",
                "a",
                "issue_summary",
                "pass",
                annotator="annotator2",
                candidate_value="v1-other",
            ),
        ]

        with pytest.raises(calibrate.CalibrationBindingError) as excinfo:
            compute_iaa_ceiling(labels)

        assert excinfo.value.mismatches == (("cal-001", "a", "issue_summary"),)

    def test_only_owner_annotator_present_raises(self):
        labels = [make_label("cal-001", "a", "issue_summary", "pass", annotator="owner")]

        with pytest.raises(calibrate.DualAnnotationError, match="exactly 2 annotators"):
            compute_iaa_ceiling(labels)

    def test_three_annotators_present_raises(self):
        labels = [
            make_label(
                "cal-001", "a", "issue_summary", "pass", annotator="owner", candidate_value="v"
            ),
            make_label(
                "cal-001",
                "a",
                "issue_summary",
                "pass",
                annotator="annotator2",
                candidate_value="v",
            ),
            make_label(
                "cal-001",
                "a",
                "issue_summary",
                "pass",
                annotator="annotator3",
                candidate_value="v",
            ),
        ]

        with pytest.raises(calibrate.DualAnnotationError, match="exactly 2 annotators"):
            compute_iaa_ceiling(labels)

    def test_owner_annotator_missing_raises(self):
        labels = [
            make_label(
                "cal-001", "a", "issue_summary", "pass", annotator="annotator2", candidate_value="v"
            ),
            make_label(
                "cal-001", "a", "issue_summary", "pass", annotator="annotator3", candidate_value="v"
            ),
        ]

        with pytest.raises(calibrate.DualAnnotationError, match="owner"):
            compute_iaa_ceiling(labels)


class TestComputeIaaCeilingKeyRestriction:
    """``keys`` (population-parity invariant, owner-ruled 2026-07-09):
    restricts the ceiling computation to a given key subset -- the same
    valid, judge-error-free population judge kappa is computed over."""

    def test_keys_param_restricts_which_keys_are_used(self, monkeypatch):
        captured: list[list[str]] = []
        real_cohens_kappa = agreement_module.cohens_kappa

        def _spy(a, b, *, clusters=None, **kwargs):
            captured.append(list(clusters))
            return real_cohens_kappa(a, b, clusters=clusters, **kwargs)

        monkeypatch.setattr(calibrate, "cohens_kappa", _spy)

        labels = _dual_labels(
            {
                f"cal-{i:03d}": ("fail" if i == 2 else "pass", "fail" if i == 2 else "pass")
                for i in range(1, 7)
            }
        )
        all_keys = tuple(
            sorted({(lbl.item_id, lbl.candidate, lbl.field) for lbl in labels})
        )
        restricted = all_keys[:-1]  # drop exactly one key

        compute_iaa_ceiling(labels, keys=restricted, n_resamples=50, seed=0)

        assert len(captured[0]) == len(restricted) == len(all_keys) - 1

    def test_omitted_keys_uses_full_doubly_labeled_set(self, monkeypatch):
        captured: list[list[str]] = []
        real_cohens_kappa = agreement_module.cohens_kappa

        def _spy(a, b, *, clusters=None, **kwargs):
            captured.append(list(clusters))
            return real_cohens_kappa(a, b, clusters=clusters, **kwargs)

        monkeypatch.setattr(calibrate, "cohens_kappa", _spy)

        labels = _dual_labels(
            {
                f"cal-{i:03d}": ("fail" if i == 2 else "pass", "fail" if i == 2 else "pass")
                for i in range(1, 7)
            }
        )

        compute_iaa_ceiling(labels, n_resamples=50, seed=0)

        assert len(captured[0]) == 6


# --------------------------------------------------------------------------
# Gold resolution -- dual-annotation upgrade, 2026-07-09.
# --------------------------------------------------------------------------


class TestResolveGoldLabels:
    def test_agreement_uses_owners_verdict_with_agreement_source(self):
        labels = _dual_labels({"cal-001": ("pass", "pass"), "cal-002": ("fail", "fail")})

        gold = resolve_gold_labels(labels)

        assert {(g.item_id, g.verdict, g.source) for g in gold} == {
            ("cal-001", "pass", "agreement"),
            ("cal-002", "fail", "agreement"),
        }

    def test_disagreement_with_adjudication_uses_adjudicated_verdict(self):
        labels = _dual_labels({"cal-001": ("pass", "pass"), "cal-002": ("pass", "fail")})
        labels.append(
            make_label(
                "cal-002",
                "a",
                "issue_summary",
                "fail",
                round_="adjudication",
                annotator="owner",
                candidate_value="cal-002-value",
            )
        )

        gold = resolve_gold_labels(labels)

        by_item = {g.item_id: g for g in gold}
        assert by_item["cal-001"].source == "agreement"
        assert by_item["cal-002"].verdict == "fail"
        assert by_item["cal-002"].source == "adjudication"

    def test_disagreement_without_adjudication_raises_error_naming_keys(self):
        labels = _dual_labels({"cal-001": ("pass", "fail"), "cal-002": ("pass", "pass")})

        with pytest.raises(calibrate.DualAnnotationError) as excinfo:
            resolve_gold_labels(labels)

        assert "('cal-001', 'a', 'issue_summary')" in str(excinfo.value)

    def test_incomplete_coverage_raises_before_checking_disagreements(self):
        labels = _dual_labels({"cal-001": ("pass", "pass"), "cal-002": ("pass", "pass")})
        # Drop annotator2's row for cal-002 only -- annotator2 is still
        # present (via cal-001), so this is incomplete coverage, not a wrong
        # annotator count.
        labels = [
            label
            for label in labels
            if not (label.annotator == "annotator2" and label.item_id == "cal-002")
        ]

        with pytest.raises(
            calibrate.DualAnnotationError, match="second annotator labels incomplete"
        ):
            resolve_gold_labels(labels)

    def test_adjudication_hash_mismatch_raises_binding_error(self):
        labels = _dual_labels({"cal-001": ("pass", "fail")})
        labels.append(
            make_label(
                "cal-001",
                "a",
                "issue_summary",
                "fail",
                round_="adjudication",
                annotator="owner",
                candidate_value="a-different-output-entirely",
            )
        )

        with pytest.raises(calibrate.CalibrationBindingError) as excinfo:
            resolve_gold_labels(labels)

        assert excinfo.value.mismatches == (("cal-001", "a", "issue_summary"),)

    def test_duplicate_owner_label_for_same_key_raises(self):
        labels = [
            make_label("cal-001", "a", "issue_summary", "pass", annotator="owner", label_id="l1"),
            make_label("cal-001", "a", "issue_summary", "fail", annotator="owner", label_id="l2"),
            make_label("cal-001", "a", "issue_summary", "pass", annotator="annotator2"),
        ]

        with pytest.raises(ValueError, match="duplicate"):
            resolve_gold_labels(labels)

    def test_n_adjudicated_count_via_gold_source(self):
        labels = _dual_labels(
            {"cal-001": ("pass", "fail"), "cal-002": ("pass", "pass"), "cal-003": ("fail", "pass")}
        )
        for item_id, verdict in (("cal-001", "pass"), ("cal-003", "fail")):
            labels.append(
                make_label(
                    item_id,
                    "a",
                    "issue_summary",
                    verdict,
                    round_="adjudication",
                    annotator="owner",
                    candidate_value=f"{item_id}-value",
                )
            )

        gold = resolve_gold_labels(labels)

        n_adjudicated = sum(1 for g in gold if g.source == "adjudication")
        assert n_adjudicated == 2

    def test_adjudication_row_from_non_owner_raises_naming_the_row(self):
        """Finding: a round='adjudication' row is only ever looked up keyed
        to the owner (spec §5), so a stray non-owner adjudication row was
        previously filtered out invisibly instead of raising."""

        labels = _dual_labels({"cal-001": ("pass", "fail")})
        labels.append(
            make_label(
                "cal-001",
                "a",
                "issue_summary",
                "fail",
                round_="adjudication",
                annotator="annotator2",
                candidate_value="cal-001-value",
            )
        )

        with pytest.raises(calibrate.DualAnnotationError) as excinfo:
            resolve_gold_labels(labels)

        message = str(excinfo.value)
        assert "non-owner" in message
        assert "annotator2" in message
        assert "cal-001" in message

    def test_adjudication_row_on_agreed_key_raises_naming_the_key(self):
        """Finding: an owner adjudication row keyed to a triple where the two
        annotators already agree was previously hash-checked but its verdict
        silently discarded -- a later edit to either initial row could then
        silently pick up this stale row as gold. It must raise instead."""

        labels = _dual_labels({"cal-001": ("pass", "pass")})
        labels.append(
            make_label(
                "cal-001",
                "a",
                "issue_summary",
                "fail",
                round_="adjudication",
                annotator="owner",
                candidate_value="cal-001-value",
            )
        )

        with pytest.raises(calibrate.DualAnnotationError) as excinfo:
            resolve_gold_labels(labels)

        message = str(excinfo.value)
        assert "already agree" in message
        assert "('cal-001', 'a', 'issue_summary')" in message

    def test_adjudication_row_outside_shared_keys_raises_naming_the_key(self):
        """Finding: an adjudication row keyed to a triple outside the shared
        initial key set (a typo'd item_id/candidate/field) was previously
        ignored entirely, while the disagreement it was meant to resolve
        went on to fail as "unadjudicated" -- misdirecting the fix. It must
        be named directly instead."""

        labels = _dual_labels({"cal-001": ("pass", "fail")})
        labels.append(
            make_label(
                "cal-999",
                "a",
                "issue_summary",
                "fail",
                round_="adjudication",
                annotator="owner",
                candidate_value="cal-999-value",
            )
        )

        with pytest.raises(calibrate.DualAnnotationError) as excinfo:
            resolve_gold_labels(labels)

        message = str(excinfo.value)
        assert "outside the shared initial-round key set" in message
        assert "('cal-999', 'a', 'issue_summary')" in message

    def test_adjudication_round_reports_all_three_violations_together(self):
        """The whole adjudication round is validated up front (finding): all
        three violation kinds present at once are reported in a single
        error, not just the first one encountered."""

        labels = _dual_labels({"cal-001": ("pass", "fail"), "cal-002": ("pass", "pass")})
        labels.append(
            make_label(
                "cal-001",
                "a",
                "issue_summary",
                "fail",
                round_="adjudication",
                annotator="annotator2",
                candidate_value="cal-001-value",
            )
        )
        labels.append(
            make_label(
                "cal-002",
                "a",
                "issue_summary",
                "fail",
                round_="adjudication",
                annotator="owner",
                candidate_value="cal-002-value",
            )
        )
        labels.append(
            make_label(
                "cal-999",
                "a",
                "issue_summary",
                "fail",
                round_="adjudication",
                annotator="owner",
                candidate_value="cal-999-value",
            )
        )

        with pytest.raises(calibrate.DualAnnotationError) as excinfo:
            resolve_gold_labels(labels)

        message = str(excinfo.value)
        assert "non-owner" in message
        assert "already agree" in message
        assert "outside the shared initial-round key set" in message


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
                round_="adjudication",
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
        self_consistency=SelfConsistencyResult(
            n_triples=20,
            repeats=3,
            flip_rate=0.05,
            flipped_triples=(("cal-007", "a", "issue_summary"),),
        ),
        ceiling=None,
        warnings=(),
        n_adjudicated=0,
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
        assert certificate.ceiling_kappa_ci is None
        assert certificate.n_adjudicated == 0
        assert certificate.label_file_hash == "hash-1"
        assert certificate.date == date(2026, 6, 1)

    def test_ceiling_kappa_and_ci_populated_when_result_has_ceiling(self):
        result = _sample_result(
            ceiling=KappaResult(kappa=0.9, ci=(0.8, 0.95), raw_agreement=0.95, prevalence=0.6),
            n_adjudicated=2,
        )

        certificate = build_certificate(result)

        assert certificate.ceiling_kappa == pytest.approx(0.9)
        assert certificate.ceiling_kappa_ci == pytest.approx((0.8, 0.95))
        assert certificate.n_adjudicated == 2

    def test_perturbation_fields_default_to_none_when_absent(self):
        result = _sample_result()

        certificate = build_certificate(result)

        assert certificate.n_perturbed is None
        assert certificate.achieved_fail_prevalence is None
        assert certificate.real_only_kappa is None
        assert certificate.real_only_kappa_ci is None
        assert certificate.perturbed_rows_passed_by_gold is None

    def test_perturbation_fields_populated_when_present(self):
        result = _sample_result(
            n_perturbed=4,
            achieved_fail_prevalence=0.22,
            real_only_kappa=KappaResult(
                kappa=0.65, ci=(0.4, 0.82), raw_agreement=0.9, prevalence=0.7
            ),
            perturbed_rows_passed_by_gold=1,
        )

        certificate = build_certificate(result)

        assert certificate.n_perturbed == 4
        assert certificate.achieved_fail_prevalence == pytest.approx(0.22)
        assert certificate.real_only_kappa == pytest.approx(0.65)
        assert certificate.real_only_kappa_ci == pytest.approx((0.4, 0.82))
        assert certificate.perturbed_rows_passed_by_gold == 1


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

        assert "Human-Human Agreement Ceiling" not in actual

    def test_renders_ceiling_section_labeled_as_human_human_agreement(self):
        result = _sample_result(
            ceiling=KappaResult(kappa=0.9, ci=(0.8, 0.95), raw_agreement=0.95, prevalence=0.6),
            n_adjudicated=2,
        )

        actual = render_calibration_report(result)

        assert "Human-Human Agreement Ceiling" in actual
        assert "the human-human agreement ceiling" in actual
        assert "indicates estimation noise, not a super-human judge" in actual
        assert "Adjudicated disagreements: 2" in actual

    def test_renders_bootstrap_disclosures_when_present(self):
        result = _sample_result(warnings=("Omitted 5 of 10000 bootstrap replicates (fixture)",))

        actual = render_calibration_report(result)

        assert "Bootstrap Disclosures" in actual
        assert "Omitted 5 of 10000 bootstrap replicates (fixture)" in actual

    def test_omits_bootstrap_disclosures_section_when_no_warnings(self):
        actual = render_calibration_report(_sample_result(warnings=()))

        assert "Bootstrap Disclosures" not in actual

    def test_excluded_counts_rendered(self):
        actual = render_calibration_report(_sample_result(judge_errors_excluded=2))

        assert "2 judge error(s)" in actual
        assert "population-parity invariant" in actual

    def test_omits_perturbation_section_when_no_probe_set_used(self):
        actual = render_calibration_report(_sample_result(n_perturbed=None))

        assert "Perturbation Probe Set" not in actual

    def test_renders_perturbation_section_when_probe_set_used(self):
        result = _sample_result(
            n_perturbed=4,
            achieved_fail_prevalence=0.22,
            real_only_kappa=KappaResult(
                kappa=0.65, ci=(0.4, 0.82), raw_agreement=0.9, prevalence=0.7
            ),
            perturbed_rows_passed_by_gold=1,
        )

        actual = render_calibration_report(result)

        assert "Perturbation Probe Set" in actual
        assert "Overlaid rows (n_perturbed): 4" in actual
        assert "22.0%" in actual
        assert "Real-only κ" in actual
        assert "0.650" in actual
        assert "never replacing it as the decision statistic" in actual
        assert "Perturbed rows the resolved gold still passed: 1" in actual


# --------------------------------------------------------------------------
# run_calibration end-to-end (synthetic RunArtifacts + fake judge).
# --------------------------------------------------------------------------


def _calibration_fixture():
    """6 calibration items, 2 candidates, 2 fields = 24 triples total, EVERY
    key labeled by both annotators (population-parity invariant, 2026-07-09:
    a judged key with no label from either annotator is now a loud
    ``DualAnnotationError``, not a silently-excluded "unlabeled" judgment --
    see ``TestRunCalibrationIntegration``/``TestRunCalibrationOffline``'s
    dedicated tests for that behavior). One key, ("cal-006", "b",
    "issue_summary"), is still a JUDGE error (verdict is ``None``) -- the ONE
    tolerated gap, excluded from judge kappa, the ceiling kappa, AND
    ``n_adjudicated`` alike, while still labeled normally by both annotators
    like every other key. One "fail" pair per candidate keeps neither
    candidate's subset collapsed to a single category; every other key has
    perfect (gold-verdict == judge-verdict) agreement.

    Dual-annotation upgrade (2026-07-09): every labeled key gets BOTH an
    ``"owner"`` and an ``"annotator2"`` row, mirroring each other exactly
    (same verdict, same candidate_value) -- perfect inter-annotator agreement
    by construction, so gold always resolves via spontaneous agreement
    (``n_adjudicated == 0``) and the human-human ceiling is exactly 1.0,
    keeping every pre-upgrade assertion about judge-vs-gold agreement intact.
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
    judge_error = ("cal-006", "b", "issue_summary")

    labels: list[CalibrationLabel] = []
    verdict_table: dict[str, str] = {}
    for item_id in item_ids:
        for candidate in ("a", "b"):
            for f in ("issue_summary", "requested_action"):
                key = (item_id, candidate, f)
                value = cv(item_id, candidate, f)
                label_verdict = "fail" if key in fail_pairs else "pass"
                for annotator in ("owner", "annotator2"):
                    labels.append(
                        make_label(
                            item_id,
                            candidate,
                            f,
                            label_verdict,
                            candidate_value=value,
                            annotator=annotator,
                        )
                    )
                if key == judge_error:
                    verdict_table[value] = "__error__"
                else:
                    verdict_table[value] = label_verdict  # judge agrees with gold

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
        assert set(result.per_candidate) == {"a", "b"}
        assert result.per_candidate["a"].kappa == pytest.approx(1.0)
        assert result.per_candidate["b"].kappa == pytest.approx(1.0)
        assert result.self_consistency.n_triples == 20  # default self-consistency n
        # Ceiling is now unconditional (dual-annotation upgrade): both
        # annotators mirror each other exactly in this fixture -> kappa 1.0,
        # zero adjudicated disagreements. The one judge error (excluded from
        # judge kappa above) is ALSO excluded from the ceiling's population
        # (population-parity invariant, 2026-07-09) -- see
        # TestPopulationParityInvariant for a test that proves this directly.
        assert result.ceiling is not None
        assert result.ceiling.kappa == pytest.approx(1.0)
        assert result.n_adjudicated == 0
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

    def test_disagreement_with_adjudication_reflected_in_n_adjudicated(self):
        """One key's second annotator disagrees with the owner; an
        adjudication row resolves it back to the owner's original verdict
        (which the judge already agrees with) -- ``n_adjudicated`` counts it,
        and the judge-agreement kappa is unaffected."""

        run_a, run_b, labels, judge = _calibration_fixture()
        target_key = ("cal-001", "a", "issue_summary")
        target_value = f"{target_key[0]}-{target_key[1]}-{target_key[2]}-value"

        modified_labels = [
            label.model_copy(update={"verdict": "fail"})
            if (label.item_id, label.candidate, label.field) == target_key
            and label.annotator == "annotator2"
            else label
            for label in labels
        ]
        adjudication = make_label(
            *target_key,
            "pass",
            round_="adjudication",
            annotator="owner",
            candidate_value=target_value,
            label_id="lbl-cal-001-a-issue_summary-adjudication",
        )

        result = run_calibration(
            run_a=run_a,
            run_b=run_b,
            labels=[*modified_labels, adjudication],
            judge=judge,
            label_file_hash="fixture-hash",
            n_resamples=50,
            seed=0,
        )

        assert result.n_adjudicated == 1
        assert result.overall.kappa == pytest.approx(1.0)  # judge still agrees with gold

    def test_empty_labels_raises_dual_annotation_error(self):
        run_a, run_b, _labels, judge = _calibration_fixture()

        with pytest.raises(calibrate.DualAnnotationError, match="exactly 2 annotators"):
            run_calibration(
                run_a=run_a,
                run_b=run_b,
                labels=[],
                judge=judge,
                label_file_hash="fixture-hash",
                n_resamples=50,
            )

    def test_labels_defect_raises_before_any_judge_invocation(self):
        """Finding 2: every DualAnnotationError/CalibrationBindingError
        ``run_calibration`` can raise via ``resolve_gold_labels`` is
        computable from ``labels`` alone -- it must fail before
        ``judge_triples`` spends a single judge call, not after (previously,
        the full judge spend over both candidates happened first and was
        never persisted on failure, re-burning it on every labels-file fix
        iteration)."""

        run_a, run_b, labels, _unused_judge = _calibration_fixture()
        # Break dual-annotation coverage entirely -- keep only the owner's
        # rows, so resolve_gold_labels raises "exactly 2 annotators".
        broken_labels = [label for label in labels if label.annotator == "owner"]

        client = _KeyedJudgeClient(verdict_for=lambda candidate_value, idx: "pass")
        judge = Judge(client)

        with pytest.raises(calibrate.DualAnnotationError, match="exactly 2 annotators"):
            run_calibration(
                run_a=run_a,
                run_b=run_b,
                labels=broken_labels,
                judge=judge,
                label_file_hash="fixture-hash",
                n_resamples=50,
            )

        assert client.calls == []

    def test_gold_resolves_but_no_overlap_with_judged_triples_raises(self):
        """Case 1 (population-parity invariant, 2026-07-09): dual-annotation
        coverage is satisfied, but for keys that don't exist among the
        judged triples at all -- this is now a loud DualAnnotationError
        naming the orphan key, not the old generic "no gold label matched"
        ValueError."""

        run_a, run_b, _labels, judge = _calibration_fixture()
        orphan_key = ("nonexistent-item", "a", "issue_summary")
        orphan_labels = [
            make_label(*orphan_key, "pass", annotator="owner", candidate_value="orphan-value"),
            make_label(
                *orphan_key, "pass", annotator="annotator2", candidate_value="orphan-value"
            ),
        ]

        with pytest.raises(calibrate.DualAnnotationError, match="no corresponding judgment"):
            run_calibration(
                run_a=run_a,
                run_b=run_b,
                labels=orphan_labels,
                judge=judge,
                label_file_hash="fixture-hash",
                n_resamples=50,
            )

    def test_judged_key_unlabeled_by_both_annotators_raises(self):
        """Case 2 (population-parity invariant, 2026-07-09): a judged key
        that neither annotator labeled must raise DualAnnotationError --
        replaces the old unlabeled_excluded tolerance."""

        run_a, run_b, labels, judge = _calibration_fixture()
        dropped_key = ("cal-001", "a", "issue_summary")
        remaining_labels = [
            label
            for label in labels
            if (label.item_id, label.candidate, label.field) != dropped_key
        ]

        with pytest.raises(calibrate.DualAnnotationError, match="labeled by neither annotator"):
            run_calibration(
                run_a=run_a,
                run_b=run_b,
                labels=remaining_labels,
                judge=judge,
                label_file_hash="fixture-hash",
                n_resamples=50,
            )


class TestPopulationParityInvariant:
    """Owner-ruled correction, 2026-07-09: judge kappa (overall and per-
    candidate), the ceiling kappa, and n_adjudicated must all be computed
    over exactly the same paired, validly-judged key set. A judge error on
    one key is the one tolerated gap -- it must shrink the population
    behind every one of those numbers together, not just judge kappa."""

    def test_judge_error_shrinks_judge_kappa_and_ceiling_populations_together(self, monkeypatch):
        call_lengths: list[int] = []
        real_cohens_kappa = agreement_module.cohens_kappa

        def _spy(a, b, *, clusters=None, **kwargs):
            call_lengths.append(len(a))
            return real_cohens_kappa(a, b, clusters=clusters, **kwargs)

        monkeypatch.setattr(calibrate, "cohens_kappa", _spy)

        run_a, run_b, labels, judge = _calibration_fixture()
        result = run_calibration(
            run_a=run_a,
            run_b=run_b,
            labels=labels,
            judge=judge,
            label_file_hash="fixture-hash",
            n_resamples=50,
            seed=0,
        )

        # Calls, in order: overall judge kappa, per-candidate "a", per-
        # candidate "b", then the ceiling kappa.
        assert len(call_lengths) == 4
        overall_n, cand_a_n, cand_b_n, ceiling_n = call_lengths
        # 24 doubly-labeled keys total, minus the fixture's one judge error
        # (candidate "b", so candidate "a"'s subset of 12 is untouched).
        assert overall_n == 23
        assert cand_a_n == 12
        assert cand_b_n == 11
        # The ceiling's population is IDENTICAL to judge kappa's overall
        # population -- the same judge error shrank both together.
        assert ceiling_n == overall_n == 23
        assert result.n_adjudicated == 0


# --------------------------------------------------------------------------
# run_calibration / run_calibration_offline with a fail-probe perturbation
# set (D2 amendment 2026-07-10).
# --------------------------------------------------------------------------


@dataclass
class _FailProbeFixture:
    run_a: RunArtifact
    run_b: RunArtifact
    probe_run_a: RunArtifact
    probe_run_b: RunArtifact
    labels: list[CalibrationLabel]
    judge: Judge
    client: _KeyedJudgeClient
    overlay: list[PerturbationOverlay]
    caught_key: tuple[str, str, str]
    uncaught_key: tuple[str, str, str]
    caught_perturbed_value: str
    uncaught_perturbed_value: str


def _fail_probe_fixture() -> _FailProbeFixture:
    """2 real calibration items (cal-001/cal-002) x 2 candidates x 2 fields =
    8 main triples, one "fail" pair per candidate so neither candidate's
    subset is single-category -- everything else "pass", both annotators and
    the judge agreeing everywhere (n_adjudicated == 0).

    2 fail-probe items (cal-101/cal-102) x 2 candidates x 2 fields = 8 probe
    triples. The overlay perturbs exactly two of them: ``caught_key``'s
    corrupted text is correctly flagged "fail" by both annotators and the
    judge (a caught perturbation); ``uncaught_key``'s corrupted text is
    still labeled/judged "pass" (the disclosed ``perturbed_rows_passed_by_
    gold`` case -- a perturbation the human standard did not flag). The
    remaining 6 probe triples keep their real output, labeled/judged "pass".

    Combined valid population: 16 keys, 3 "fail" (2 main + 1 probe-caught),
    13 "pass" -- judge agrees with gold everywhere (both overall and
    real-only kappa are well-defined and equal to 1.0).
    """

    main_item_ids = ["cal-001", "cal-002"]
    main_items = [make_item(i) for i in main_item_ids]

    def cv(item_id: str, candidate: str, field: str, *, tag: str = "value") -> str:
        return f"{item_id}-{candidate}-{field}-{tag}"

    rows_by_candidate: dict[str, list[RunRow]] = {"a": [], "b": []}
    for item_id in main_item_ids:
        for candidate in ("a", "b"):
            rows_by_candidate[candidate].append(
                make_row(
                    item_id,
                    0,
                    issue_summary=cv(item_id, candidate, "issue_summary"),
                    requested_action=cv(item_id, candidate, "requested_action"),
                )
            )
    run_a = make_run_artifact("a", main_items, rows_by_candidate["a"])
    run_b = make_run_artifact("b", main_items, rows_by_candidate["b"])

    main_fail_pairs = {("cal-001", "a", "issue_summary"), ("cal-002", "b", "requested_action")}

    probe_item_ids = ["cal-101", "cal-102"]
    probe_items = [make_item(i) for i in probe_item_ids]
    probe_rows_by_candidate: dict[str, list[RunRow]] = {"a": [], "b": []}
    for item_id in probe_item_ids:
        for candidate in ("a", "b"):
            probe_rows_by_candidate[candidate].append(
                make_row(
                    item_id,
                    0,
                    issue_summary=cv(item_id, candidate, "issue_summary", tag="real"),
                    requested_action=cv(item_id, candidate, "requested_action", tag="real"),
                )
            )
    probe_run_a = make_run_artifact("a", probe_items, probe_rows_by_candidate["a"])
    probe_run_b = make_run_artifact("b", probe_items, probe_rows_by_candidate["b"])

    caught_key = ("cal-101", "a", "issue_summary")
    uncaught_key = ("cal-102", "b", "requested_action")
    caught_perturbed_value = "corrupted-cal-101-a-issue_summary"
    uncaught_perturbed_value = "corrupted-cal-102-b-requested_action"

    overlay = [
        make_overlay_row(
            *caught_key, caught_perturbed_value, corruption_type="ungrounded_addition"
        ),
        make_overlay_row(
            *uncaught_key, uncaught_perturbed_value, corruption_type="dropped_essential"
        ),
    ]

    labels: list[CalibrationLabel] = []
    verdict_table: dict[str, str] = {}

    for item_id in main_item_ids:
        for candidate in ("a", "b"):
            for field_name in ("issue_summary", "requested_action"):
                key = (item_id, candidate, field_name)
                value = cv(item_id, candidate, field_name)
                verdict = "fail" if key in main_fail_pairs else "pass"
                for annotator in ("owner", "annotator2"):
                    labels.append(
                        make_label(
                            item_id, candidate, field_name, verdict,
                            candidate_value=value, annotator=annotator,
                        )
                    )
                verdict_table[value] = verdict

    for item_id in probe_item_ids:
        for candidate in ("a", "b"):
            for field_name in ("issue_summary", "requested_action"):
                key = (item_id, candidate, field_name)
                real_value = cv(item_id, candidate, field_name, tag="real")
                if key == caught_key:
                    seen_value, verdict = caught_perturbed_value, "fail"
                elif key == uncaught_key:
                    seen_value, verdict = uncaught_perturbed_value, "pass"
                else:
                    seen_value, verdict = real_value, "pass"
                for annotator in ("owner", "annotator2"):
                    labels.append(
                        make_label(
                            item_id, candidate, field_name, verdict,
                            candidate_value=seen_value, annotator=annotator,
                        )
                    )
                verdict_table[seen_value] = verdict

    def _verdict_for(candidate_value: str, idx: int) -> str:
        return verdict_table[candidate_value]

    client = _KeyedJudgeClient(verdict_for=_verdict_for)
    judge = Judge(client)

    return _FailProbeFixture(
        run_a=run_a,
        run_b=run_b,
        probe_run_a=probe_run_a,
        probe_run_b=probe_run_b,
        labels=labels,
        judge=judge,
        client=client,
        overlay=overlay,
        caught_key=caught_key,
        uncaught_key=uncaught_key,
        caught_perturbed_value=caught_perturbed_value,
        uncaught_perturbed_value=uncaught_perturbed_value,
    )


class TestRunCalibrationWithFailProbeSet:
    def test_overlay_applied_before_judging_judge_sees_corrupted_text(self):
        fx = _fail_probe_fixture()

        calibrate_module_run_calibration = run_calibration(
            run_a=fx.run_a,
            run_b=fx.run_b,
            labels=fx.labels,
            judge=fx.judge,
            label_file_hash="fixture-hash",
            probe_run_a=fx.probe_run_a,
            probe_run_b=fx.probe_run_b,
            perturbation_overlay=fx.overlay,
            n_resamples=50,
            seed=0,
        )

        judged_prompts = fx.client.calls
        real_value = "cal-101-a-issue_summary-real"
        assert any(fx.caught_perturbed_value in p for p in judged_prompts)
        assert not any(real_value in p for p in judged_prompts)
        assert calibrate_module_run_calibration.n_perturbed == 2

    def test_non_overlaid_probe_rows_keep_real_run_output(self):
        fx = _fail_probe_fixture()

        run_calibration(
            run_a=fx.run_a,
            run_b=fx.run_b,
            labels=fx.labels,
            judge=fx.judge,
            label_file_hash="fixture-hash",
            probe_run_a=fx.probe_run_a,
            probe_run_b=fx.probe_run_b,
            perturbation_overlay=fx.overlay,
            n_resamples=50,
            seed=0,
        )

        non_overlaid_real_value = "cal-101-b-issue_summary-real"
        assert any(non_overlaid_real_value in p for p in fx.client.calls)

    def test_disclosure_fields_populated_correctly(self):
        fx = _fail_probe_fixture()

        result = run_calibration(
            run_a=fx.run_a,
            run_b=fx.run_b,
            labels=fx.labels,
            judge=fx.judge,
            label_file_hash="fixture-hash",
            probe_run_a=fx.probe_run_a,
            probe_run_b=fx.probe_run_b,
            perturbation_overlay=fx.overlay,
            n_resamples=200,
            seed=0,
        )

        assert result.n_perturbed == 2
        assert result.achieved_fail_prevalence == pytest.approx(3 / 16)
        assert result.perturbed_rows_passed_by_gold == 1
        assert result.real_only_kappa is not None
        assert result.real_only_kappa.kappa == pytest.approx(1.0)
        assert result.overall.kappa == pytest.approx(1.0)
        assert result.probe_item_ids == frozenset({"cal-101", "cal-102"})

    def test_population_sizes_span_the_union_correctly(self, monkeypatch):
        """Overall/per-candidate/ceiling kappa run over the FULL 16-key union
        (main + probe); real-only kappa is separately restricted to the
        8-key main-only population -- proving both populations are computed
        distinctly, never conflated."""

        call_lengths: list[int] = []
        real_cohens_kappa = agreement_module.cohens_kappa

        def _spy(a, b, *, clusters=None, **kwargs):
            call_lengths.append(len(a))
            return real_cohens_kappa(a, b, clusters=clusters, **kwargs)

        monkeypatch.setattr(calibrate, "cohens_kappa", _spy)
        fx = _fail_probe_fixture()

        run_calibration(
            run_a=fx.run_a,
            run_b=fx.run_b,
            labels=fx.labels,
            judge=fx.judge,
            label_file_hash="fixture-hash",
            probe_run_a=fx.probe_run_a,
            probe_run_b=fx.probe_run_b,
            perturbation_overlay=fx.overlay,
            n_resamples=50,
            seed=0,
        )

        # Order: overall, per-candidate "a", per-candidate "b", ceiling, real-only.
        assert len(call_lengths) == 5
        overall_n, cand_a_n, cand_b_n, ceiling_n, real_only_n = call_lengths
        assert overall_n == 16
        assert cand_a_n == 8
        assert cand_b_n == 8
        assert ceiling_n == 16
        assert real_only_n == 8

    def test_omitting_probe_runs_leaves_disclosure_fields_none(self):
        """Backward compatibility: calling run_calibration without any
        probe_run_a/probe_run_b/perturbation_overlay argument reproduces
        pre-amendment behavior exactly -- every fail-probe disclosure field
        stays at its default None/empty."""

        run_a, run_b, labels, judge = _calibration_fixture()

        result = run_calibration(
            run_a=run_a,
            run_b=run_b,
            labels=labels,
            judge=judge,
            label_file_hash="fixture-hash",
            n_resamples=50,
            seed=0,
        )

        assert result.n_perturbed is None
        assert result.achieved_fail_prevalence is None
        assert result.real_only_kappa is None
        assert result.perturbed_rows_passed_by_gold is None
        assert result.probe_item_ids == frozenset()

    def test_overlay_validation_error_propagates_before_any_judge_call(self):
        """A bad overlay (key targeting the original emails file) must raise
        PerturbationOverlayError before spending a single judge call --
        mirroring the existing labels-defect-before-judge-call precedent."""

        fx = _fail_probe_fixture()
        bad_overlay = [make_overlay_row("cal-001", "a", "issue_summary", "x")]

        with pytest.raises(calibrate.PerturbationOverlayError, match="original emails file"):
            run_calibration(
                run_a=fx.run_a,
                run_b=fx.run_b,
                labels=fx.labels,
                judge=fx.judge,
                label_file_hash="fixture-hash",
                probe_run_a=fx.probe_run_a,
                probe_run_b=fx.probe_run_b,
                perturbation_overlay=bad_overlay,
                n_resamples=50,
            )

        assert fx.client.calls == []

    def test_missing_label_for_probe_key_raises_dual_annotation_error(self):
        """Population parity spans the union unchanged (spec §5): a probe
        key with no label from either annotator must raise the SAME
        DualAnnotationError a main-set gap would."""

        fx = _fail_probe_fixture()
        dropped_key = ("cal-101", "a", "requested_action")
        remaining_labels = [
            label
            for label in fx.labels
            if (label.item_id, label.candidate, label.field) != dropped_key
        ]

        with pytest.raises(calibrate.DualAnnotationError, match="labeled by neither annotator"):
            run_calibration(
                run_a=fx.run_a,
                run_b=fx.run_b,
                labels=remaining_labels,
                judge=fx.judge,
                label_file_hash="fixture-hash",
                probe_run_a=fx.probe_run_a,
                probe_run_b=fx.probe_run_b,
                perturbation_overlay=fx.overlay,
                n_resamples=50,
            )


class TestRunCalibrationOfflineWithFailProbeSet:
    def _judgments_file(self, result: CalibrationResult) -> calibrate.JudgmentsFile:
        return calibrate.JudgmentsFile(
            judge_version=result.judge_version,
            written_at="2026-07-10T00:00:00+00:00",
            judgments=tuple(
                calibrate.judgment_records_from_judged(
                    result.judged_triples,
                    judge_version=result.judge_version,
                    probe_item_ids=result.probe_item_ids,
                )
            ),
            self_consistency=result.self_consistency_records,
        )

    def test_offline_reproduces_the_same_disclosure_as_live(self):
        fx = _fail_probe_fixture()
        live_result = run_calibration(
            run_a=fx.run_a,
            run_b=fx.run_b,
            labels=fx.labels,
            judge=fx.judge,
            label_file_hash="fixture-hash",
            probe_run_a=fx.probe_run_a,
            probe_run_b=fx.probe_run_b,
            perturbation_overlay=fx.overlay,
            n_resamples=200,
            seed=0,
        )
        judgments_file = self._judgments_file(live_result)

        offline_result = calibrate.run_calibration_offline(
            judgments=judgments_file,
            labels=fx.labels,
            label_file_hash="fixture-hash",
            perturbation_overlay=fx.overlay,
            n_resamples=200,
            seed=0,
        )

        assert offline_result.n_perturbed == live_result.n_perturbed == 2
        assert offline_result.achieved_fail_prevalence == pytest.approx(
            live_result.achieved_fail_prevalence
        )
        assert offline_result.perturbed_rows_passed_by_gold == (
            live_result.perturbed_rows_passed_by_gold
        )
        assert offline_result.real_only_kappa is not None
        assert live_result.real_only_kappa is not None
        assert offline_result.real_only_kappa.kappa == pytest.approx(
            live_result.real_only_kappa.kappa
        )
        assert offline_result.probe_item_ids == live_result.probe_item_ids

    def test_backward_compatible_when_no_judgment_is_marked_is_probe(self):
        """A judgments.jsonl produced entirely pre-amendment (every record's
        ``is_probe`` defaulting False) must still recompute cleanly, with
        every fail-probe disclosure field at its None default -- passing an
        empty overlay reproduces pre-amendment behavior exactly."""

        run_a, run_b, labels, judge = _calibration_fixture()
        live_result = run_calibration(
            run_a=run_a, run_b=run_b, labels=labels, judge=judge,
            label_file_hash="fixture-hash", n_resamples=50, seed=0,
        )
        judgments_file = calibrate.JudgmentsFile(
            judge_version=live_result.judge_version,
            written_at="2026-07-10T00:00:00+00:00",
            judgments=tuple(
                calibrate.judgment_records_from_judged(
                    live_result.judged_triples, judge_version=live_result.judge_version
                )
            ),
            self_consistency=live_result.self_consistency_records,
        )
        assert all(not j.is_probe for j in judgments_file.judgments)

        offline_result = calibrate.run_calibration_offline(
            judgments=judgments_file, labels=labels, label_file_hash="fixture-hash",
            n_resamples=50, seed=0,
        )

        assert offline_result.n_perturbed is None
        assert offline_result.achieved_fail_prevalence is None
        assert offline_result.real_only_kappa is None
        assert offline_result.perturbed_rows_passed_by_gold is None

    def test_offline_overlay_validation_error_propagates(self):
        fx = _fail_probe_fixture()
        live_result = run_calibration(
            run_a=fx.run_a,
            run_b=fx.run_b,
            labels=fx.labels,
            judge=fx.judge,
            label_file_hash="fixture-hash",
            probe_run_a=fx.probe_run_a,
            probe_run_b=fx.probe_run_b,
            perturbation_overlay=fx.overlay,
            n_resamples=50,
            seed=0,
        )
        judgments_file = self._judgments_file(live_result)
        bad_overlay = [make_overlay_row("cal-001", "a", "issue_summary", "x")]

        with pytest.raises(calibrate.PerturbationOverlayError, match="original emails file"):
            calibrate.run_calibration_offline(
                judgments=judgments_file,
                labels=fx.labels,
                label_file_hash="fixture-hash",
                perturbation_overlay=bad_overlay,
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

    def test_is_probe_defaults_false_when_omitted(self):
        """Backward compatibility (D2 amendment 2026-07-10): omitting
        probe_item_ids reproduces pre-amendment behavior exactly -- every
        record's is_probe is False."""

        email = make_item("cal-001").email
        triple = Triple("cal-001", "a", "issue_summary", email, "ref", "the-value")
        judged = [JudgedTriple(triple, verdict="pass", error=None, rationale="ok")]

        records = calibrate.judgment_records_from_judged(judged, judge_version="jv-x")

        assert records[0].is_probe is False

    def test_is_probe_stamped_from_probe_item_ids(self):
        email = make_item("x").email
        real_triple = Triple("cal-001", "a", "issue_summary", email, "ref", "real-value")
        probe_triple = Triple("cal-101", "a", "issue_summary", email, "ref", "probe-value")
        judged = [
            JudgedTriple(real_triple, verdict="pass", error=None, rationale="ok"),
            JudgedTriple(probe_triple, verdict="fail", error=None, rationale="ok"),
        ]

        records = calibrate.judgment_records_from_judged(
            judged, judge_version="jv-x", probe_item_ids={"cal-101", "cal-102"}
        )

        by_item = {r.item_id: r for r in records}
        assert by_item["cal-001"].is_probe is False
        assert by_item["cal-101"].is_probe is True


class TestJudgmentRecordIsProbeBackwardCompat:
    def test_positional_construction_without_is_probe_defaults_false(self):
        """Every existing positional-arg JudgmentRecord() construction in
        this test module (8 args) must keep working unchanged -- proves the
        new is_probe field doesn't break the old shape."""

        record = calibrate.JudgmentRecord(
            "cal-001", "a", "issue_summary", "pass", None, "ok", "deadbeef" * 8, "jv"
        )

        assert record.is_probe is False

    def test_load_judgments_jsonl_row_without_is_probe_key_defaults_false(self, tmp_path):
        """A judgments.jsonl written BEFORE this amendment has no 'is_probe'
        key in its judgment rows at all -- JudgmentRecord(**row) must still
        succeed, defaulting is_probe to False."""

        path = tmp_path / "judgments.jsonl"
        lines = [
            json.dumps({"kind": "meta", "judge_version": "jv-1", "written_at": "2026-01-01"}),
            json.dumps(
                {
                    "kind": "judgment",
                    "item_id": "cal-001",
                    "candidate": "a",
                    "field": "issue_summary",
                    "verdict": "pass",
                    "error": None,
                    "rationale": "ok",
                    "output_sha256": "h" * 64,
                    "judge_version": "jv-1",
                    # no "is_probe" key -- pre-amendment shape.
                }
            ),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        loaded = calibrate.load_judgments_jsonl(path)

        assert loaded.judgments[0].is_probe is False

    def test_write_load_round_trip_preserves_is_probe_true(self, tmp_path):
        path = tmp_path / "judgments.jsonl"
        record = calibrate.JudgmentRecord(
            "cal-101", "a", "issue_summary", "fail", None, "ok", "a" * 64, "jv-1", is_probe=True
        )

        calibrate.write_judgments_jsonl(
            path, judgments=[record], self_consistency=[], judge_version="jv-1"
        )
        loaded = calibrate.load_judgments_jsonl(path)

        assert loaded.judgments[0].is_probe is True


class TestPairJudgmentsWithLabels:
    def test_pairs_matching_and_excludes_judge_errors(self):
        judgments = [
            calibrate.JudgmentRecord(
                "cal-001", "a", "issue_summary", "pass", None, "ok",
                calibrate.hash_output("v1"), "jv",
            ),
            calibrate.JudgmentRecord(
                "cal-001", "a", "requested_action", None, "refusal", None,
                calibrate.hash_output("v2"), "jv",
            ),
        ]
        gold = [
            make_gold("cal-001", "a", "issue_summary", "pass", candidate_value="v1"),
            make_gold("cal-001", "a", "requested_action", "pass", candidate_value="v2"),
        ]

        paired, judge_errors, valid_keys = calibrate.pair_judgments_with_labels(judgments, gold)

        assert len(paired) == 1
        assert judge_errors == 1
        assert valid_keys == (("cal-001", "a", "issue_summary"),)

    def test_judgment_unlabeled_by_both_annotators_raises(self):
        """Case 2 (population-parity invariant, 2026-07-09), offline unit
        level: a persisted judgment whose key was labeled by neither
        annotator raises DualAnnotationError -- replaces the old
        unlabeled_excluded tolerance."""

        judgments = [
            calibrate.JudgmentRecord(
                "cal-002", "a", "issue_summary", "pass", None, "ok",
                calibrate.hash_output("v3"), "jv",
            ),
        ]

        with pytest.raises(calibrate.DualAnnotationError, match="labeled by neither annotator"):
            calibrate.pair_judgments_with_labels(judgments, [])

    def test_persisted_hash_mismatch_raises_binding_error(self):
        judgments = [
            calibrate.JudgmentRecord(
                "cal-001", "a", "issue_summary", "pass", None, "ok", "deadbeef" * 8, "jv",
            ),
        ]
        gold = [make_gold("cal-001", "a", "issue_summary", "pass", candidate_value="v1")]

        with pytest.raises(calibrate.CalibrationBindingError) as excinfo:
            calibrate.pair_judgments_with_labels(judgments, gold)

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
        assert offline_result.self_consistency.n_triples == live_result.self_consistency.n_triples
        assert offline_result.self_consistency.flip_rate == pytest.approx(
            live_result.self_consistency.flip_rate
        )
        assert offline_result.date == live_result.date
        assert offline_result.ceiling is not None
        assert live_result.ceiling is not None
        assert offline_result.ceiling.kappa == pytest.approx(live_result.ceiling.kappa)
        assert offline_result.n_adjudicated == live_result.n_adjudicated
        # Offline never re-derives judged_triples/self_consistency_records --
        # it already consumed a persisted copy of them.
        assert offline_result.judged_triples == ()
        assert offline_result.self_consistency_records == ()

    def test_offline_never_touches_judge_or_client(self):
        """No ``judge``/``Judge`` argument even exists on this call --
        proves by signature, not just by absence of a fake, that zero calls
        can be made. The retired ``--retest`` flag's ``retest`` kwarg is gone
        too -- dual annotation is unconditional (module docstring)."""

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
        assert "retest" not in sig.parameters

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

        with pytest.raises(calibrate.DualAnnotationError, match="exactly 2 annotators"):
            calibrate.run_calibration_offline(
                judgments=judgments_file, labels=[], label_file_hash="fixture-hash",
                n_resamples=50,
            )

    def test_gold_resolves_but_no_overlap_with_judgments_raises(self):
        """Case 1 (population-parity invariant, 2026-07-09): now a loud
        DualAnnotationError naming the orphan key, not the old generic "no
        gold label matched" ValueError."""

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
        orphan_labels = [
            make_label(
                "nonexistent-item", "a", "issue_summary", "pass",
                annotator="owner", candidate_value="orphan-value",
            ),
            make_label(
                "nonexistent-item", "a", "issue_summary", "pass",
                annotator="annotator2", candidate_value="orphan-value",
            ),
        ]

        with pytest.raises(calibrate.DualAnnotationError, match="no corresponding judgment"):
            calibrate.run_calibration_offline(
                judgments=judgments_file, labels=orphan_labels, label_file_hash="fixture-hash",
                n_resamples=50,
            )

    def test_judgment_unlabeled_by_both_annotators_raises(self):
        """Case 2 (population-parity invariant, 2026-07-09), offline path: a
        persisted judgment whose key was labeled by neither annotator must
        raise DualAnnotationError, not be silently excluded as
        'unlabeled_excluded'."""

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

        dropped_key = ("cal-001", "a", "issue_summary")
        remaining_labels = [
            label
            for label in labels
            if (label.item_id, label.candidate, label.field) != dropped_key
        ]

        with pytest.raises(calibrate.DualAnnotationError, match="labeled by neither annotator"):
            calibrate.run_calibration_offline(
                judgments=judgments_file,
                labels=remaining_labels,
                label_file_hash="fixture-hash",
                n_resamples=50,
            )

    def test_never_judged_disagreement_now_raises_population_parity_error(self):
        """Population-parity invariant (owner-ruled, 2026-07-09) supersedes
        the OLD behavior this test used to pin (a label for a never-judged
        item silently moving the offline-recomputed ceiling): a gold label
        for an item that was NEVER judged at all (absent from the persisted
        judgment set) now raises DualAnnotationError naming the orphan key,
        because every certificate number must be computed over exactly one,
        paired, validly-judged population -- the ceiling is no longer free to
        read a broader population than judge kappa does."""

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

        # Owner and annotator2 disagree on "cal-999" (never judged at all --
        # it isn't among run_a/run_b's items) -- an owner adjudication row
        # resolves the disagreement so resolve_gold_labels itself succeeds,
        # but "cal-999" still has no corresponding judgment.
        extra_owner = make_label(
            "cal-999", "a", "issue_summary", "pass",
            annotator="owner", candidate_value="extra-value", label_id="lbl-extra-owner",
        )
        extra_other = make_label(
            "cal-999", "a", "issue_summary", "fail",
            annotator="annotator2", candidate_value="extra-value", label_id="lbl-extra-annotator2",
        )
        extra_adjudication = make_label(
            "cal-999", "a", "issue_summary", "pass",
            round_="adjudication", annotator="owner",
            candidate_value="extra-value", label_id="lbl-extra-adjudication",
        )

        with pytest.raises(calibrate.DualAnnotationError, match="no corresponding judgment"):
            calibrate.run_calibration_offline(
                judgments=judgments_file,
                labels=[*labels, extra_owner, extra_other, extra_adjudication],
                label_file_hash="fixture-hash",
                n_resamples=200,
                seed=0,
            )


# --------------------------------------------------------------------------
# `eval calibrate` CLI wiring (typer.testing.CliRunner). No live API calls:
# candidate runs are pre-seeded via run_eval with fakes, the judge is a
# scripted fake, and TraceContext is replaced with an always-traced fake
# where a test needs happy-path behavior to proceed.
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
    """Both annotators' labels matching ``_CalibrationCandidateClient``'s
    output exactly -- perfect agreement, both between the annotators AND
    against the judge -- with one "fail" per candidate (so neither
    candidate's per-candidate subset collapses to a single category).

    Dual-annotation upgrade (2026-07-09): ``"owner"`` and ``"annotator2"``
    mirror each other exactly here, so gold always resolves via spontaneous
    agreement (``n_adjudicated == 0``) and the certificate's human-human
    ceiling comes out to 1.0 -- keeping every pre-upgrade CLI assertion about
    judge-vs-gold agreement intact."""

    fail_pairs = {(item_ids[0], "a", "issue_summary"), (item_ids[1], "b", "requested_action")}
    labels: list[CalibrationLabel] = []
    for item_id in item_ids:
        for candidate in ("a", "b"):
            for f in ("issue_summary", "requested_action"):
                verdict = "fail" if (item_id, candidate, f) in fail_pairs else "pass"
                value = _happy_path_candidate_value(item_id, candidate, f)
                for annotator in ("owner", "annotator2"):
                    labels.append(
                        make_label(
                            item_id,
                            candidate,
                            f,
                            verdict,
                            candidate_value=value,
                            annotator=annotator,
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
        # Finding 2: labels are now validated before the Langfuse fail-fast
        # check, so this test uses a labels file that clears dual-annotation
        # validation cleanly (both annotators agreeing on one key) -- an
        # empty/defective labels file would now raise DualAnnotationError
        # first, which is exercised separately in this class's own
        # ``test_labels_only_error_fails_before_trace_context_or_client_construction``.
        labels_path = _write_labels(
            tmp_path / "labels.jsonl",
            [
                make_label(
                    "cal-001", "a", "issue_summary", "pass", annotator="owner", candidate_value="v"
                ),
                make_label(
                    "cal-001",
                    "a",
                    "issue_summary",
                    "pass",
                    annotator="annotator2",
                    candidate_value="v",
                ),
            ],
        )
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

    def test_labels_only_error_fails_before_trace_context_or_client_construction(
        self, tmp_path, monkeypatch
    ):
        """Finding 2: a DualAnnotationError/CalibrationBindingError raised by
        ``resolve_gold_labels`` is computable from ``labels.jsonl`` alone --
        it must fail before ``TraceContext.for_run`` or any candidate/judge
        client construction, costing zero API calls and no client setup."""

        monkeypatch.chdir(tmp_path)

        emails_path = _write_dataset(tmp_path / "emails.jsonl", [_cli_item("cal-001")])
        # Only ONE annotator's initial labels -- DualAnnotationError,
        # resolvable from labels.jsonl alone, well before any tracing/client
        # work.
        labels_path = _write_labels(
            tmp_path / "labels.jsonl",
            [make_label("cal-001", "a", "issue_summary", "pass", annotator="owner")],
        )
        config_path = _write_calibrate_config(tmp_path / "config.yaml")

        class _ForbiddenTraceContext:
            @staticmethod
            def for_run(config: object, reportable: bool, **kwargs: object) -> object:
                raise AssertionError(
                    "TraceContext.for_run must not be called before labels-only validation"
                )

        def _forbid_build_model_key(label, config):
            raise AssertionError(
                "_build_model_key must not be called before labels-only validation"
            )

        def _forbid_build_judge_client(config):
            raise AssertionError(
                "_build_judge_client must not be called before labels-only validation"
            )

        monkeypatch.setattr(cli, "TraceContext", _ForbiddenTraceContext)
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
        assert "exactly 2 annotators" in result.output


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
        # Dual annotation is automatic (no --retest flag needed): the ceiling
        # section and its adjudication count appear in every report now.
        assert "Human-Human Agreement Ceiling" in result.output
        assert "Adjudicated disagreements: 0" in result.output

        cert_path = tmp_path / "data" / "calibration" / "certificate.json"
        assert cert_path.exists()
        certificate = Certificate.model_validate(json.loads(cert_path.read_text(encoding="utf-8")))
        assert certificate.verdict == "adequate"
        assert certificate.overall_kappa == pytest.approx(1.0)
        assert certificate.per_candidate_kappa_ci is not None
        assert certificate.ceiling_kappa == pytest.approx(1.0)
        assert certificate.ceiling_kappa_ci is not None
        assert certificate.n_adjudicated == 0
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

    def test_disagreement_with_adjudication_reflected_in_certificate(self, tmp_path, monkeypatch):
        """No flag required (dual annotation is automatic, 2026-07-09): one
        annotator2 verdict is flipped to disagree with the owner, and an
        owner adjudication row resolves it back -- the certificate discloses
        exactly one adjudicated disagreement."""

        monkeypatch.chdir(tmp_path)
        item_ids = ["cal-001", "cal-002", "cal-003"]
        items = [_cli_item(i) for i in item_ids]
        emails_path = _write_dataset(tmp_path / "emails.jsonl", items)

        labels = _happy_path_labels(item_ids)
        target_item, target_candidate, target_field = "cal-003", "a", "issue_summary"
        target_value = _happy_path_candidate_value(target_item, target_candidate, target_field)
        modified_labels = [
            label.model_copy(update={"verdict": "fail"})
            if (
                label.item_id == target_item
                and label.candidate == target_candidate
                and label.field == target_field
                and label.annotator == "annotator2"
            )
            else label
            for label in labels
        ]
        adjudication = make_label(
            target_item,
            target_candidate,
            target_field,
            "pass",
            round_="adjudication",
            annotator="owner",
            candidate_value=target_value,
            label_id="lbl-cal-003-a-issue_summary-adjudication",
        )
        labels_path = _write_labels(tmp_path / "labels.jsonl", [*modified_labels, adjudication])
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
            ],
        )

        assert result.exit_code == 0, result.output
        assert "the human-human agreement ceiling" in result.output
        assert "Adjudicated disagreements: 1" in result.output
        cert_path = tmp_path / "data" / "calibration" / "certificate.json"
        certificate = Certificate.model_validate(json.loads(cert_path.read_text(encoding="utf-8")))
        assert certificate.ceiling_kappa is not None
        assert certificate.n_adjudicated == 1


class TestCalibrateCLIIncompleteSecondAnnotator:
    def test_missing_second_annotator_labels_gives_clean_exit_one(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        item_ids = ["cal-001", "cal-002", "cal-003"]
        items = [_cli_item(i) for i in item_ids]
        emails_path = _write_dataset(tmp_path / "emails.jsonl", items)

        full_labels = _happy_path_labels(item_ids)
        dropped_key = (item_ids[0], "a", "issue_summary")
        incomplete_labels = [
            label
            for label in full_labels
            if not (
                label.annotator == "annotator2"
                and (label.item_id, label.candidate, label.field) == dropped_key
            )
        ]
        labels_path = _write_labels(tmp_path / "labels.jsonl", incomplete_labels)
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
            ],
        )

        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "second annotator labels incomplete" in result.output.lower()


class TestCalibrateCLILiveOutputBinding:
    def test_live_output_hash_mismatch_gives_clean_exit_one(self, tmp_path, monkeypatch):
        """``CalibrationBindingError`` on the LIVE path (finding F1,
        ``pair_with_labels``) -- mirrors ``TestCalibrateCLIOffline``'s own
        ``test_output_hash_mismatch_gives_clean_exit_one`` for the offline
        path (``pair_judgments_with_labels``): a label whose
        ``output_sha256`` no longer matches the candidate output it is keyed
        to must exit cleanly, never with a traceback, even when both
        candidate runs are reused rather than freshly executed."""

        monkeypatch.chdir(tmp_path)
        item_ids = ["cal-001", "cal-002", "cal-003"]
        items = [_cli_item(i) for i in item_ids]
        emails_path = _write_dataset(tmp_path / "emails.jsonl", items)

        labels = _happy_path_labels(item_ids)
        corrupted_first = labels[0].model_copy(update={"output_sha256": "0" * 64})
        labels_path = _write_labels(tmp_path / "labels.jsonl", [corrupted_first, *labels[1:]])
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

        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert corrupted_first.item_id in result.output


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
        # Every certificate field, not just the headline kappa/CI/verdict:
        # --offline recomputes the FULL certificate (finding F2, spec AC5)
        # from persisted judgments, so nothing else may drift either.
        assert offline_certificate.overall_kappa == pytest.approx(live_certificate.overall_kappa)
        assert offline_certificate.kappa_ci == pytest.approx(live_certificate.kappa_ci)
        assert offline_certificate.verdict == live_certificate.verdict
        assert offline_certificate.judge_version == live_certificate.judge_version
        assert offline_certificate.label_file_hash == live_certificate.label_file_hash
        assert offline_certificate.ceiling_kappa == live_certificate.ceiling_kappa
        assert offline_certificate.ceiling_kappa_ci == live_certificate.ceiling_kappa_ci
        assert offline_certificate.n_adjudicated == live_certificate.n_adjudicated

        assert (
            offline_certificate.per_candidate_kappa.keys()
            == live_certificate.per_candidate_kappa.keys()
        )
        for label, kappa in live_certificate.per_candidate_kappa.items():
            assert offline_certificate.per_candidate_kappa[label] == pytest.approx(kappa)

        assert live_certificate.per_candidate_kappa_ci is not None
        assert offline_certificate.per_candidate_kappa_ci is not None
        assert (
            offline_certificate.per_candidate_kappa_ci.keys()
            == live_certificate.per_candidate_kappa_ci.keys()
        )
        for label, ci in live_certificate.per_candidate_kappa_ci.items():
            assert offline_certificate.per_candidate_kappa_ci[label] == pytest.approx(ci)

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


# --------------------------------------------------------------------------
# `eval calibrate` fail-probe perturbation set (D2 amendment 2026-07-10):
# --fail-probe-emails / --perturbations, live and --offline.
# --------------------------------------------------------------------------


def _fail_probe_cli_fixture(tmp_path: Path) -> dict:
    """Writes a main calibration set (3 items) + a fail-probe set (2 items)
    + a 1-row overlay perturbing (cal-101, a, issue_summary), seeds both
    candidate runs via run_eval directly, and returns everything a CLI
    invocation needs. The overlaid row's gold verdict is "fail" (a caught
    perturbation); every other probe key keeps its real candidate output,
    labeled "pass"."""

    item_ids = ["cal-001", "cal-002", "cal-003"]
    items = [_cli_item(i) for i in item_ids]
    emails_path = _write_dataset(tmp_path / "emails.jsonl", items)

    probe_item_ids = ["cal-101", "cal-102"]
    probe_items = [_cli_item(i) for i in probe_item_ids]
    fail_probe_path = _write_dataset(tmp_path / "emails-fail-probe.jsonl", probe_items)

    perturbed_key = ("cal-101", "a", "issue_summary")
    perturbed_value = "ungrounded claim about a refund"
    overlay_row = PerturbationOverlay(
        item_id=perturbed_key[0],
        candidate=perturbed_key[1],
        field=perturbed_key[2],
        perturbed_value=perturbed_value,
        corruption_type="ungrounded_addition",
        rationale="probe CLI test",
    )
    perturbations_path = tmp_path / "perturbations.jsonl"
    perturbations_path.write_text(
        json.dumps(overlay_row.model_dump(mode="json")) + "\n", encoding="utf-8"
    )

    labels = _happy_path_labels(item_ids)
    for probe_item in probe_item_ids:
        for candidate in ("a", "b"):
            for field_name in ("issue_summary", "requested_action"):
                key = (probe_item, candidate, field_name)
                if key == perturbed_key:
                    value, verdict = perturbed_value, "fail"
                else:
                    value, verdict = (
                        _happy_path_candidate_value(probe_item, candidate, field_name),
                        "pass",
                    )
                for annotator in ("owner", "annotator2"):
                    labels.append(
                        make_label(
                            probe_item, candidate, field_name, verdict,
                            candidate_value=value, annotator=annotator,
                        )
                    )
    labels_path = _write_labels(tmp_path / "labels.jsonl", labels)
    config_path = _write_calibrate_config(tmp_path / "config.yaml", k=1)

    cfg = load_config(config_path)
    effective_cfg, calib_items = cli._resolve_calibration_dataset(cfg, emails_path)
    _seed_calibration_runs(effective_cfg, calib_items)
    probe_effective_cfg, probe_calib_items = cli._resolve_calibration_dataset(
        effective_cfg, fail_probe_path
    )
    _seed_calibration_runs(probe_effective_cfg, probe_calib_items)

    def judge_factory() -> _KeyedJudgeClient:
        def verdict_for(candidate_value: str, idx: int) -> str:
            if candidate_value == perturbed_value:
                return "fail"
            parts = candidate_value.rsplit("-", 2)
            if len(parts) == 3:
                probe_item_id, candidate, short_field = parts
                field = "issue_summary" if short_field == "issue" else "requested_action"
                fail_pairs = {
                    ("cal-001", "a", "issue_summary"),
                    ("cal-002", "b", "requested_action"),
                }
                if (probe_item_id, candidate, field) in fail_pairs:
                    return "fail"
            return "pass"

        return _KeyedJudgeClient(verdict_for=verdict_for)

    return {
        "emails_path": emails_path,
        "labels_path": labels_path,
        "config_path": config_path,
        "fail_probe_path": fail_probe_path,
        "perturbations_path": perturbations_path,
        "judge_factory": judge_factory,
        "item_ids": item_ids,
        "probe_item_ids": probe_item_ids,
    }


def _forbid_build_model_key(label, config):
    raise AssertionError("_build_model_key must not be called -- both runs are reused")


class TestCalibrateCLIFailProbeSet:
    def test_absent_probe_files_leave_certificate_perturbation_fields_none(
        self, tmp_path, monkeypatch
    ):
        """Backward compatibility (D2 amendment 2026-07-10): --fail-probe-
        emails/--perturbations default to paths that don't exist under this
        tmp_path cwd -- current behavior (pre-amendment) must be unchanged."""

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
        assert "Perturbation Probe Set" not in result.output

        cert_path = tmp_path / "data" / "calibration" / "certificate.json"
        certificate = Certificate.model_validate(json.loads(cert_path.read_text(encoding="utf-8")))
        assert certificate.n_perturbed is None
        assert certificate.achieved_fail_prevalence is None
        assert certificate.real_only_kappa is None
        assert certificate.real_only_kappa_ci is None
        assert certificate.perturbed_rows_passed_by_gold is None

    def test_probe_file_present_applies_overlay_and_discloses_stats(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        fx = _fail_probe_cli_fixture(tmp_path)

        monkeypatch.setattr(cli, "TraceContext", _FakeTraceContext)
        monkeypatch.setattr(cli, "_build_model_key", _forbid_build_model_key)
        monkeypatch.setattr(cli, "_build_judge_client", lambda config: fx["judge_factory"]())

        result = cli_runner.invoke(
            app,
            [
                "calibrate",
                "--emails",
                str(fx["emails_path"]),
                "--labels",
                str(fx["labels_path"]),
                "--config",
                str(fx["config_path"]),
                "--fail-probe-emails",
                str(fx["fail_probe_path"]),
                "--perturbations",
                str(fx["perturbations_path"]),
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Perturbation Probe Set" in result.output

        cert_path = tmp_path / "data" / "calibration" / "certificate.json"
        certificate = Certificate.model_validate(json.loads(cert_path.read_text(encoding="utf-8")))
        assert certificate.n_perturbed == 1
        assert certificate.achieved_fail_prevalence is not None
        assert certificate.real_only_kappa is not None
        assert certificate.perturbed_rows_passed_by_gold == 0

        judgments_path = tmp_path / "data" / "calibration" / "judgments.jsonl"
        judgments = calibrate.load_judgments_jsonl(judgments_path)
        probe_records = [j for j in judgments.judgments if j.item_id in fx["probe_item_ids"]]
        assert len(probe_records) == 8  # 2 probe items x 2 candidates x 2 fields
        assert all(j.is_probe for j in probe_records)
        main_records = [j for j in judgments.judgments if j.item_id in fx["item_ids"]]
        assert main_records and all(not j.is_probe for j in main_records)

    def test_overlay_targeting_main_file_gives_clean_exit_one(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        item_ids = ["cal-001", "cal-002", "cal-003"]
        items = [_cli_item(i) for i in item_ids]
        emails_path = _write_dataset(tmp_path / "emails.jsonl", items)
        labels_path = _write_labels(tmp_path / "labels.jsonl", _happy_path_labels(item_ids))
        config_path = _write_calibrate_config(tmp_path / "config.yaml", k=1)

        bad_overlay_row = PerturbationOverlay(
            item_id="cal-001",
            candidate="a",
            field="issue_summary",
            perturbed_value="x",
            corruption_type="entity_swap",
            rationale="bad -- targets the original emails file",
        )
        perturbations_path = tmp_path / "perturbations.jsonl"
        perturbations_path.write_text(
            json.dumps(bad_overlay_row.model_dump(mode="json")) + "\n", encoding="utf-8"
        )

        cfg = load_config(config_path)
        effective_cfg, calib_items = cli._resolve_calibration_dataset(cfg, emails_path)
        _seed_calibration_runs(effective_cfg, calib_items)

        monkeypatch.setattr(cli, "TraceContext", _FakeTraceContext)
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
                "--perturbations",
                str(perturbations_path),
            ],
        )

        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "original emails file" in result.output


class TestCalibrateCLIOfflineFailProbeSet:
    def test_offline_recompute_matches_live_perturbation_fields(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        fx = _fail_probe_cli_fixture(tmp_path)

        monkeypatch.setattr(cli, "TraceContext", _FakeTraceContext)
        monkeypatch.setattr(cli, "_build_model_key", _forbid_build_model_key)
        monkeypatch.setattr(cli, "_build_judge_client", lambda config: fx["judge_factory"]())

        live_result = cli_runner.invoke(
            app,
            [
                "calibrate",
                "--emails",
                str(fx["emails_path"]),
                "--labels",
                str(fx["labels_path"]),
                "--config",
                str(fx["config_path"]),
                "--fail-probe-emails",
                str(fx["fail_probe_path"]),
                "--perturbations",
                str(fx["perturbations_path"]),
            ],
        )
        assert live_result.exit_code == 0, live_result.output
        cert_path = tmp_path / "data" / "calibration" / "certificate.json"
        live_certificate = Certificate.model_validate(
            json.loads(cert_path.read_text(encoding="utf-8"))
        )
        judgments_path = tmp_path / "data" / "calibration" / "judgments.jsonl"

        def _forbid(*args: object, **kwargs: object) -> None:
            raise AssertionError("must not be called in --offline mode")

        monkeypatch.setattr(cli, "_build_model_key", _forbid)
        monkeypatch.setattr(cli, "_build_judge_client", _forbid)
        _forbid_real_provider_client_construction(monkeypatch)

        offline_result = cli_runner.invoke(
            app,
            [
                "calibrate",
                "--offline",
                "--labels",
                str(fx["labels_path"]),
                "--judgments",
                str(judgments_path),
                "--perturbations",
                str(fx["perturbations_path"]),
            ],
        )

        assert offline_result.exit_code == 0, offline_result.output
        offline_certificate = Certificate.model_validate(
            json.loads(cert_path.read_text(encoding="utf-8"))
        )
        assert offline_certificate.n_perturbed == live_certificate.n_perturbed
        assert offline_certificate.achieved_fail_prevalence == pytest.approx(
            live_certificate.achieved_fail_prevalence
        )
        assert offline_certificate.real_only_kappa == pytest.approx(
            live_certificate.real_only_kappa
        )
        assert offline_certificate.perturbed_rows_passed_by_gold == (
            live_certificate.perturbed_rows_passed_by_gold
        )

    def test_offline_overlay_targeting_main_file_gives_clean_exit_one(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        labels_path, judgments_path = _seed_live_judgments(tmp_path)

        bad_overlay_row = PerturbationOverlay(
            item_id="cal-001",
            candidate="a",
            field="issue_summary",
            perturbed_value="x",
            corruption_type="entity_swap",
            rationale="bad -- targets the original emails file, no probe set was ever used",
        )
        perturbations_path = tmp_path / "perturbations.jsonl"
        perturbations_path.write_text(
            json.dumps(bad_overlay_row.model_dump(mode="json")) + "\n", encoding="utf-8"
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
                "--perturbations",
                str(perturbations_path),
            ],
        )

        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "original emails file" in result.output
