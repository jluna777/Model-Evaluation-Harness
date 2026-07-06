"""Golden-file + behavioral tests for the three report renderers (T10).

Golden fixtures live under ``tests/fixtures/reports/``: hand-built run
artifacts (manifest + rows.jsonl, same shape T08 persists), committed
certificate fixtures, and the frozen expected markdown for each of the three
"happy path" renders (byte-identical comparison). Banner-state behavior
(inadequate certificate, missing certificate, untraced, reportable-without-
certificate) is covered separately by lighter, non-golden assertion tests --
per the ticket, byte-equality on one happy fixture is not sufficient
coverage for those states.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from harness.config import load_config
from harness.reports import (
    CandidateGateResult,
    GateSummaryData,
    MissingCertificateError,
    _pearson_r,
    _sparse_delta_warning,
    render_compare_report,
    render_gate_summary,
    render_run_report,
)
from harness.runner import RunDir, load_run
from harness.schema import Certificate
from harness.scoring.composite import CompositeMode

FIXTURES = Path(__file__).parents[1] / "fixtures" / "reports"
DEFAULT_CONFIG_PATH = Path(__file__).parents[2] / "configs" / "default.yaml"

# A small, fixed n_resamples keeps the golden-file bootstrap/permutation
# calls fast and, combined with a fixed seed, fully reproducible.
_SEED = 0
_N_RESAMPLES = 500


def _certificate(name: str) -> Certificate:
    path = FIXTURES / f"certificate_{name}.json"
    return Certificate.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _run_report_artifact():
    return load_run(RunDir(path=FIXTURES / "run_report" / "run"))


def _compare_artifacts():
    artifact_a = load_run(RunDir(path=FIXTURES / "compare_report" / "run_a"))
    artifact_b = load_run(RunDir(path=FIXTURES / "compare_report" / "run_b"))
    return artifact_a, artifact_b


def _candidate_gate_result(**overrides) -> CandidateGateResult:
    defaults = dict(
        label="a",
        verdict="pass",
        delta=0.8,
        delta_ci=(-1.2, 2.5),
        p_value=0.42,
        m_nonzero=18,
        min_attainable_p=2.0**-18,
        permutation_method="exact",
        mde=6.1,
        judge_error_excluded=1,
        adversarial_delta=-2.0,
        adversarial_guardrail_tripped=False,
        usage_candidate={"input_tokens": 50_000, "output_tokens": 20_000},
        usage_judge={"input_tokens": 80_000, "output_tokens": 30_000},
        untraced=False,
    )
    defaults.update(overrides)
    return CandidateGateResult(**defaults)


def _gate_summary_data(**overrides) -> GateSummaryData:
    config = load_config(DEFAULT_CONFIG_PATH)
    defaults = dict(
        certificate=_certificate("adequate"),
        reportable=True,
        composite_mode=CompositeMode.FULL_7,
        margin=config.gate.margin,
        alpha=config.gate.alpha,
        k=config.k,
        price_snapshot=config.price_snapshot,
        candidates=(
            _candidate_gate_result(label="a", verdict="pass"),
            _candidate_gate_result(
                label="b",
                verdict="fail",
                delta=-3.4,
                delta_ci=(-6.1, -1.0),
                p_value=0.012,
                m_nonzero=20,
                min_attainable_p=2.0**-20,
                mde=5.8,
                judge_error_excluded=2,
                adversarial_delta=-11.5,
                adversarial_guardrail_tripped=True,
                usage_candidate={"input_tokens": 52_000, "output_tokens": 21_000},
                usage_judge={"input_tokens": 81_000, "output_tokens": 31_000},
            ),
        ),
        overall_verdict="fail",
    )
    defaults.update(overrides)
    return GateSummaryData(**defaults)


# --------------------------------------------------------------------------
# Golden-file comparisons ("happy path" fixtures): byte-identical AND
# content-complete for every binding element in the ticket's content lists.
# --------------------------------------------------------------------------


class TestRunReportGolden:
    def test_byte_identical_to_expected(self):
        artifact = _run_report_artifact()
        certificate = _certificate("adequate")

        actual = render_run_report(
            artifact,
            certificate=certificate,
            reportable=True,
            seed=_SEED,
            n_resamples=_N_RESAMPLES,
        )

        expected = (FIXTURES / "run_report" / "expected_run_report.md").read_text(
            encoding="utf-8"
        )
        assert actual == expected

    def test_contains_all_binding_content_elements(self):
        actual = (FIXTURES / "run_report" / "expected_run_report.md").read_text(encoding="utf-8")

        # Certificate header (spec §5): judge version, kappa +/- CI, verdict.
        assert "Judge Calibration Certificate" in actual
        assert "fixture-judge-version-hash" in actual
        assert "Verdict: **adequate**" in actual

        # All three slice groupings with 95% BCa cluster CIs.
        assert "| nominal | 4 |" in actual
        assert "| adversarial | 4 |" in actual
        assert "| all | 8 |" in actual
        assert "95% BCa CI" in actual

        # Per-field accuracies.
        assert "Per-Field Accuracy" in actual
        assert "issue_summary | 7 | 1 |" in actual  # judge-error row excluded, disclosed

        # Per-category table.
        assert "Per-Category" in actual
        assert "| billing | 4 |" in actual

        # Variance decomposition, full + judged-only.
        assert "Full composite (FULL_7)" in actual
        assert "Judged-fields-only composite" in actual
        assert "Between-item variance" in actual
        assert "Between-replicate variance" in actual
        assert "ddof=0" in actual  # T5 population-convention footer note

        # Score-vs-length correlation.
        assert "Score-vs-Length Correlation" in actual
        assert "Pearson r" in actual


class TestCompareReportGolden:
    def test_byte_identical_to_expected(self):
        artifact_a, artifact_b = _compare_artifacts()
        certificate = _certificate("adequate")

        actual = render_compare_report(
            artifact_a,
            artifact_b,
            certificate=certificate,
            reportable=True,
            seed=_SEED,
            n_resamples=_N_RESAMPLES,
        )

        expected = (FIXTURES / "compare_report" / "expected_compare_report.md").read_text(
            encoding="utf-8"
        )
        assert actual == expected

    def test_contains_all_binding_content_elements(self):
        actual = (FIXTURES / "compare_report" / "expected_compare_report.md").read_text(
            encoding="utf-8"
        )

        assert "Judge Calibration Certificate" in actual

        # Mean delta + 95% BCa CI.
        assert "Mean delta (b - a):" in actual
        assert "95% BCa CI" in actual

        # Two-sided permutation p.
        assert "Two-sided sign-flip permutation p" in actual

        # Per-field pass-rate delta table with flip counts.
        assert "Per-Field Pass-Rate Delta" in actual
        assert "fail→pass flips" in actual
        assert "pass→fail flips" in actual
        # At least one real flip count recorded (priority: 1 fail->pass).
        assert "| priority | 75.0% | 100.0% | +25.0 | 1 | 0 |" in actual

        # Absolute scores alongside deltas.
        assert "Composite Score (absolute, alongside deltas below)" in actual
        assert "| a | 91.07 |" in actual
        assert "| b | 96.43 |" in actual


class TestGateSummaryGolden:
    def test_byte_identical_to_expected(self):
        data = _gate_summary_data()

        actual = render_gate_summary(data)

        expected = (FIXTURES / "gate_summary" / "expected_gate_summary.md").read_text(
            encoding="utf-8"
        )
        assert actual == expected

    def test_contains_all_binding_content_elements(self):
        actual = (FIXTURES / "gate_summary" / "expected_gate_summary.md").read_text(
            encoding="utf-8"
        )

        assert "Judge Calibration Certificate" in actual

        # Verdict per candidate.
        assert "### Candidate a" in actual
        assert "### Candidate b" in actual
        assert "Verdict: **PASS**" in actual
        assert "Verdict: **FAIL**" in actual

        # Delta + 90% BCa CI; one-sided p.
        assert "90% BCa CI" in actual
        assert "One-sided sign-flip permutation p" in actual

        # MDE.
        assert "MDE (α=0.05, 80% power)" in actual

        # Judge-error exclusion count.
        assert "Judge-error exclusions:" in actual

        # Adversarial delta always printed + guardrail status.
        assert "Adversarial-slice delta" in actual
        assert "not tripped" in actual
        assert "**TRIPPED**" in actual

        # Literal family false-alarm line.
        assert "two tests at α=0.05 → ≤ ~9.8% worst case" in actual

        # Config values used.
        assert "- margin: 2.0" in actual
        assert "- alpha: 0.05" in actual
        assert "- K: 3" in actual

        # Token totals + approximate cost, candidate and judge separately.
        assert "Candidate token usage:" in actual
        assert "Judge token usage:" in actual
        assert "~$" in actual

        # Relative link to docs/gate-design.md.
        assert "[docs/gate-design.md](docs/gate-design.md)" in actual


# --------------------------------------------------------------------------
# Certificate header / banner-state behavior (not the golden happy path).
# --------------------------------------------------------------------------


class TestUncalibratedBanner:
    def test_run_report_shows_uncalibrated_banner_when_dev_stage(self):
        artifact = _run_report_artifact()

        actual = render_run_report(
            artifact, certificate=None, reportable=False, seed=_SEED, n_resamples=_N_RESAMPLES
        )

        assert "UNCALIBRATED (no certificate)" in actual
        assert "Judge Calibration Certificate" not in actual

    def test_compare_report_shows_uncalibrated_banner_when_dev_stage(self):
        artifact_a, artifact_b = _compare_artifacts()

        actual = render_compare_report(
            artifact_a,
            artifact_b,
            certificate=None,
            reportable=False,
            seed=_SEED,
            n_resamples=_N_RESAMPLES,
        )

        assert "UNCALIBRATED (no certificate)" in actual

    def test_gate_summary_shows_uncalibrated_banner_when_dev_stage(self):
        data = _gate_summary_data(certificate=None, reportable=False)

        actual = render_gate_summary(data)

        assert "UNCALIBRATED (no certificate)" in actual


class TestReportableWithoutCertificateRaises:
    def test_run_report_raises(self):
        artifact = _run_report_artifact()

        with pytest.raises(MissingCertificateError):
            render_run_report(artifact, certificate=None, reportable=True)

    def test_compare_report_raises(self):
        artifact_a, artifact_b = _compare_artifacts()

        with pytest.raises(MissingCertificateError):
            render_compare_report(artifact_a, artifact_b, certificate=None, reportable=True)

    def test_gate_summary_raises(self):
        data = _gate_summary_data(certificate=None, reportable=True)

        with pytest.raises(MissingCertificateError):
            render_gate_summary(data)


class TestInadequateCertificateSurfacesDeterministic5:
    def test_run_report_flags_judged_fields_excluded(self):
        artifact = _run_report_artifact()
        certificate = _certificate("inadequate")

        actual = render_run_report(
            artifact,
            certificate=certificate,
            reportable=True,
            seed=_SEED,
            n_resamples=_N_RESAMPLES,
        )

        assert "Judged fields excluded (DETERMINISTIC_5)" in actual
        assert "Composite mode used for every aggregate below: **DETERMINISTIC_5**." in actual

    def test_compare_report_flags_judged_fields_excluded(self):
        artifact_a, artifact_b = _compare_artifacts()
        certificate = _certificate("inadequate")

        actual = render_compare_report(
            artifact_a,
            artifact_b,
            certificate=certificate,
            reportable=True,
            seed=_SEED,
            n_resamples=_N_RESAMPLES,
        )

        assert "Judged fields excluded (DETERMINISTIC_5)" in actual

    def test_gate_summary_flags_judged_fields_excluded(self):
        data = _gate_summary_data(
            certificate=_certificate("inadequate"), composite_mode=CompositeMode.DETERMINISTIC_5
        )

        actual = render_gate_summary(data)

        assert "Judged fields excluded (DETERMINISTIC_5)" in actual
        assert "Composite mode used for every figure below: **DETERMINISTIC_5**." in actual


class TestAdequateWithCaveat:
    def test_run_report_shows_caveat_note(self):
        artifact = _run_report_artifact()
        certificate = _certificate("adequate_with_caveat")

        actual = render_run_report(
            artifact,
            certificate=certificate,
            reportable=True,
            seed=_SEED,
            n_resamples=_N_RESAMPLES,
        )

        assert "adequate_with_caveat" in actual
        assert "Caveat:" in actual
        # Caveat does not exclude judged fields (only "inadequate" does).
        assert "Composite mode used for every aggregate below: **FULL_7**." in actual


class TestUntracedBanner:
    def test_run_report_shows_untraced_banner(self):
        artifact = dataclasses.replace(_run_report_artifact(), untraced=True)
        certificate = _certificate("adequate")

        actual = render_run_report(
            artifact,
            certificate=certificate,
            reportable=False,
            seed=_SEED,
            n_resamples=_N_RESAMPLES,
        )

        assert "UNTRACED" in actual

    def test_run_report_omits_untraced_banner_when_traced(self):
        artifact = _run_report_artifact()
        assert artifact.untraced is False
        certificate = _certificate("adequate")

        actual = render_run_report(
            artifact,
            certificate=certificate,
            reportable=False,
            seed=_SEED,
            n_resamples=_N_RESAMPLES,
        )

        assert "UNTRACED" not in actual

    def test_compare_report_shows_untraced_banner_for_untraced_candidate(self):
        artifact_a, artifact_b = _compare_artifacts()
        artifact_b = dataclasses.replace(artifact_b, untraced=True)
        certificate = _certificate("adequate")

        actual = render_compare_report(
            artifact_a,
            artifact_b,
            certificate=certificate,
            reportable=False,
            seed=_SEED,
            n_resamples=_N_RESAMPLES,
        )

        assert "UNTRACED" in actual
        assert "candidate b" in actual

    def test_gate_summary_shows_untraced_banner_for_untraced_candidate(self):
        data = _gate_summary_data(
            candidates=(
                _candidate_gate_result(label="a", untraced=True),
                _candidate_gate_result(label="b", verdict="fail", delta=-3.4),
            )
        )

        actual = render_gate_summary(data)

        assert "UNTRACED" in actual
        assert "candidate a" in actual


# --------------------------------------------------------------------------
# Direct unit tests for small pure helpers.
# --------------------------------------------------------------------------


class TestSparseDeltaWarning:
    @pytest.mark.parametrize("m", [0, 1, 2, 3, 4])
    def test_m_le_4_impossible_warning(self, m):
        warning = _sparse_delta_warning(m)
        assert warning is not None
        assert "no rejection is possible" in warning

    def test_m_5_all_negative_required_warning(self):
        warning = _sparse_delta_warning(5)
        assert warning is not None
        assert "all five nonzero" in warning
        assert "0.031" in warning

    @pytest.mark.parametrize("m", [6, 7, 20, 32])
    def test_m_ge_6_no_warning(self, m):
        assert _sparse_delta_warning(m) is None

    def test_gate_summary_renders_sparse_delta_warning_for_low_m(self):
        data = _gate_summary_data(
            candidates=(
                _candidate_gate_result(label="a", m_nonzero=3),
                _candidate_gate_result(label="b", verdict="fail", delta=-3.4, m_nonzero=5),
            )
        )

        actual = render_gate_summary(data)

        assert "no rejection is possible" in actual
        assert "all five nonzero" in actual


class TestPearsonR:
    def test_perfect_positive_correlation(self):
        assert _pearson_r([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)

    def test_perfect_negative_correlation(self):
        assert _pearson_r([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)

    def test_fewer_than_two_pairs_is_none(self):
        assert _pearson_r([1.0], [2.0]) is None
        assert _pearson_r([], []) is None

    def test_zero_variance_is_none(self):
        assert _pearson_r([1, 1, 1], [1, 2, 3]) is None
        assert _pearson_r([1, 2, 3], [5, 5, 5]) is None


class TestGateSummaryDataIsAPlainDataclass:
    """T16 constructs ``GateSummaryData`` directly -- this just proves it's
    a small, explicit, keyword-constructible dataclass with no hidden
    dependency on RunArtifact/baseline objects (module docstring)."""

    def test_construction_from_plain_values(self):
        config = load_config(DEFAULT_CONFIG_PATH)
        data = GateSummaryData(
            certificate=None,
            reportable=False,
            composite_mode=CompositeMode.FULL_7,
            margin=2.0,
            alpha=0.05,
            k=3,
            price_snapshot=config.price_snapshot,
            candidates=(_candidate_gate_result(),),
            overall_verdict="pass",
        )
        assert data.k == 3
        assert data.candidates[0].label == "a"
