"""CI gate decision rule, sparse-delta warnings, adversarial guardrail, exit
codes (spec §7, D3, T16).

This module is the "decide" half of the gate; ``harness.reports.
render_gate_summary`` (T10) is the "render" half -- this module computes
every number ``GateSummaryData``/``CandidateGateResult`` carry and never
renders markdown itself (mirrors ``reports.py``'s purity contract: this
module is pure over already-loaded ``BaselineFile``/``RunArtifact``/
``Config``/``Certificate`` objects, no API calls, no filesystem access,
except ``update_baseline`` which is deliberately the one impure entry point,
since generating a baseline necessarily drives real candidate/judge calls).

**Two layers, deliberately separated (see tests/unit/gate/test_gate.py's
module docstring for the corresponding test split):**

1. Data extraction -- ``nominal_paired_deltas``, ``adversarial_composite_delta``,
   ``judge_error_rate`` -- turn a ``(BaselineFile, RunArtifact)`` pair into the
   raw numbers the decision rule needs.
2. Decision -- ``decide_candidate_result`` turns a list of per-item deltas (already
   extracted) into a ``CandidateGateResult`` via the stats modules
   (``sign_flip_test``, ``bca_ci``, ``mde``) and spec §7's fail condition.

``evaluate_gate`` composes both layers plus the fingerprint/judge-error-budget
checks into one pure orchestration function, returning a ``GateOutcome``
(0/1) or raising one of this module's own exceptions (2) -- see the module's
exit-code convention below.

**Item-level judge-error exclusion (binding precision point, spec §7 "missing
!= fail"):** unlike ``reports.py``'s ``_row_composite`` (which excludes only
the missing *field* from a row's own average, returning ``None`` only when
*every* mode-included field is missing), this module excludes the entire
*item* from the paired-delta computation the moment ANY mode-included field
is missing on ANY replicate, on EITHER side of the comparison (baseline or
run). This stricter convention is deliberate: a partial per-row average would
dilute a paired delta with an incomplete replicate; the ticket's contract is
that the whole item drops out, disclosed via an exclusion count, rather than
silently averaged over fewer samples than the other items got.

**Exit-code convention (this module's own design decision, since spec §7
enumerates measurement-error exit-2 conditions precisely but the CLI layer
also has setup-time errors spec doesn't classify):** exit 2 is reserved for
exactly the four spec-enumerated measurement-error conditions raised as
exceptions here (``MissingBaselineError``, ``FingerprintMismatchError``,
``JudgeErrorBudgetExceededError``) plus ``runner.RunAborted`` (spec §7
explicitly lists "aborted run" under measurement error for the *gate*,
distinct from ``run``/``compare``'s own exit-1 treatment of the same
exception) -- all mapped at the CLI layer (``cli.py``'s ``_gate_clean_exit``).
Every other expected/setup failure (missing tracing credentials, a missing
API key, a provider SDK construction failure, a run-config mismatch, a failed
guardrail check on ``--update-baseline``) is treated the same way ``run``/
``compare`` already treat their own expected failures: a clean one-line exit
1, since none of those are "a completed measurement whose verdict is fail"
(exit 1's OTHER meaning, "regression detected") or one of spec's four
measurement-error conditions specifically.
"""

from __future__ import annotations

import shutil
import statistics
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from harness.config import Config
from harness.gate.baseline import (
    DEFAULT_BASELINES_ROOT,
    GUARDRAIL_SE_MULTIPLIER,
    GUARDRAIL_THRESHOLD_POINTS,
    BaselineFile,
    Mismatch,
    check_fingerprint,
    check_guardrail_floor,
    fingerprint_components_from_run,
    generate_baseline,
)
from harness.prompts import EXTRACTION_PROMPT, PromptTemplate
from harness.reports import CandidateGateResult, GateSummaryData, render_gate_summary
from harness.runner import DEFAULT_RUNS_ROOT, ModelKey, RunArtifact, RunRow
from harness.schema import Certificate, GoldenItem
from harness.scoring.composite import DETERMINISTIC_FIELDS, JUDGED_FIELDS, CompositeMode, composite
from harness.stats.bootstrap import bca_ci
from harness.stats.mde import mde
from harness.stats.permutation import sign_flip_test
from harness.tracing import TraceContext

# D3/spec §7 pinned parameters -- see docs/decisions.md D3; changing these
# needs a dated decision-log amendment.
JUDGE_ERROR_RATE_BUDGET = 0.05
GATE_CI_LEVEL = 0.90
MDE_POWER = 0.80

# Mirrors scoring.composite's own private `_MODE_FIELDS` mapping, built from
# the public field-group constants -- same convention as baseline.py/
# reports.py (never reach into composite.py's private mapping directly).
_MODE_FIELDS: dict[CompositeMode, tuple[str, ...]] = {
    CompositeMode.FULL_7: DETERMINISTIC_FIELDS + JUDGED_FIELDS,
    CompositeMode.DETERMINISTIC_5: DETERMINISTIC_FIELDS,
}

DEMO_MODE_BANNER = (
    "=" * 70
    + "\nDEMO MODE -- this gate run used --seed-regression: DEGRADED_DEMO_PROMPT "
    "was applied at runtime and the fingerprint check was skipped. This is a "
    "deliberately induced regression for demonstration only -- it is never a "
    "real candidate regression and must never be treated as one.\n"
    + "=" * 70
)


# --------------------------------------------------------------------------
# Exceptions -- see module docstring's exit-code convention.
# --------------------------------------------------------------------------


class MissingBaselineError(Exception):
    """No committed baseline exists for a candidate (spec §7: baselines are
    never auto-created). Names the ``--update-baseline`` instruction and the
    prompt-bump PR instruction (attach the compare-vs-old-baseline report for
    human review) verbatim, since both are required reading at this exact
    failure point."""

    def __init__(self, label: str, path: Path | str) -> None:
        super().__init__(
            f"No committed baseline for candidate {label} at {path}. Baselines are never "
            "auto-created (spec §7): run `eval gate --update-baseline` to generate one. If "
            "this is a prompt-version bump, the update-baseline PR must attach the "
            "compare-vs-old-baseline report for human review."
        )
        self.label = label
        self.path = path


class FingerprintMismatchError(Exception):
    """A run's fingerprint components disagree with the committed baseline's
    (spec §7: any mismatch is a measurement error, never silently ignored).
    Names every differing field so the operator knows exactly what drifted,
    and states the re-baseline instruction."""

    def __init__(self, label: str, mismatches: Sequence[Mismatch]) -> None:
        details = "; ".join(
            f"{m.field}: baseline={m.baseline_value!r} run={m.run_value!r}" for m in mismatches
        )
        super().__init__(
            f"Fingerprint mismatch for candidate {label} ({details}). This run cannot be "
            "compared against the committed baseline (spec §7). If this is an intentional "
            "prompt/model/judge change, re-baseline with `eval gate --update-baseline` "
            "(attach the compare-vs-old-baseline report for human review); otherwise "
            "investigate the drift before re-running the gate."
        )
        self.label = label
        self.mismatches = tuple(mismatches)


class JudgeErrorBudgetExceededError(Exception):
    """A candidate's judge-error rate exceeded ``JUDGE_ERROR_RATE_BUDGET``
    (spec §7): a measurement error, never a candidate regression -- judge
    failures can never register as a regression (spec §7)."""

    def __init__(self, label: str, rate: float, budget: float) -> None:
        super().__init__(
            f"Judge-error rate for candidate {label} is {rate:.1%}, exceeding the "
            f"{budget:.0%} budget (spec §7): this is a measurement error, not a candidate "
            "regression. Investigate judge transport/refusal failures before re-running."
        )
        self.label = label
        self.rate = rate
        self.budget = budget


class GuardrailFloorError(Exception):
    """Raised by ``update_baseline`` when the freshly measured adversarial
    noise floor fails ``check_guardrail_floor`` (spec §7/D3): the committed
    baseline path is left untouched (never written, never overwritten)."""

    def __init__(
        self, label: str, measured_se: float, threshold_points: float, se_multiplier: float
    ) -> None:
        super().__init__(
            f"Refusing to write baseline for candidate {label}: the measured adversarial "
            f"guardrail noise floor (SE={measured_se:.2f}) does not clear the required margin "
            f"({threshold_points:.1f} points >= {se_multiplier:.0f}x SE, spec §7/D3). The "
            "committed baseline is left unchanged."
        )
        self.label = label
        self.measured_se = measured_se


# --------------------------------------------------------------------------
# Composite-mode resolution (mirrors reports.py's own private helper --
# duplicated locally rather than imported, same module-boundary convention
# baseline.py already established for _MODE_FIELDS).
# --------------------------------------------------------------------------


def _resolve_composite_mode(certificate: Certificate | None) -> CompositeMode:
    if certificate is not None and certificate.verdict == "inadequate":
        return CompositeMode.DETERMINISTIC_5
    return CompositeMode.FULL_7


# --------------------------------------------------------------------------
# Data extraction: nominal_paired_deltas, adversarial_composite_delta,
# judge_error_rate.
# --------------------------------------------------------------------------


def _item_replicate_composites(
    rows: Sequence[RunRow], item_id: str, mode: CompositeMode
) -> list[float] | None:
    """Per-replicate composite scores for ``item_id``, ordered by whatever
    order ``rows`` holds them in. ``None`` -- signalling this item must be
    excluded entirely -- iff no rows exist for it, or ANY of its replicates
    has a missing (``None``) mode-included field (module docstring's
    item-level exclusion convention)."""

    item_rows = [r for r in rows if r.item_id == item_id]
    if not item_rows:
        return None
    fields = _MODE_FIELDS[mode]
    composites: list[float] = []
    for row in item_rows:
        if any(row.field_scores[f] is None for f in fields):
            return None
        composites.append(composite(row.field_scores, mode))
    return composites


def nominal_paired_deltas(
    baseline: BaselineFile, run: RunArtifact, mode: CompositeMode
) -> tuple[list[float], int]:
    """K-averaged paired per-item deltas (``current - baseline``) over the
    nominal slice shared by both sides (spec §7). Returns ``(deltas,
    excluded_count)``: ``deltas`` has one entry per included item (in
    deterministic sorted-item-id order, for reproducible stats calls), and
    ``excluded_count`` is how many nominal items were dropped because a
    mode-included field was missing on at least one replicate, on either
    side (module docstring's item-level exclusion convention -- missing !=
    fail, spec §7).

    Items with zero delta (baseline and run agree) are NOT excluded -- they
    are legitimate zero-valued entries in ``deltas``, diluting the mean
    exactly like ``sign_flip_test``'s own zero-delta convention (its module
    docstring).
    """

    baseline_ids = {item.id for item in baseline.items if item.meta.slice == "nominal"}
    run_ids = {item.id for item in run.items if item.meta.slice == "nominal"}
    shared_ids = sorted(baseline_ids & run_ids)

    deltas: list[float] = []
    excluded = 0
    for item_id in shared_ids:
        baseline_composites = _item_replicate_composites(baseline.rows, item_id, mode)
        run_composites = _item_replicate_composites(run.rows, item_id, mode)
        if baseline_composites is None or run_composites is None:
            excluded += 1
            continue
        deltas.append(statistics.mean(run_composites) - statistics.mean(baseline_composites))
    return deltas, excluded


def _adversarial_mean_composite(
    items: Sequence[GoldenItem], rows: Sequence[RunRow], mode: CompositeMode
) -> float | None:
    """Mean composite over every adversarial-slice row (all replicates, all
    adversarial items) that has no missing mode-included field. ``None`` iff
    no such row exists (no adversarial items, or all excluded)."""

    adversarial_ids = {item.id for item in items if item.meta.slice == "adversarial"}
    fields = _MODE_FIELDS[mode]
    values = [
        composite(row.field_scores, mode)
        for row in rows
        if row.item_id in adversarial_ids and all(row.field_scores[f] is not None for f in fields)
    ]
    return statistics.mean(values) if values else None


def adversarial_composite_delta(
    baseline: BaselineFile, run: RunArtifact, mode: CompositeMode
) -> float:
    """The adversarial-slice composite delta (``current - baseline``) backing
    the coarse, non-statistical guardrail (spec §7) -- always computed and
    printed, whether or not it trips. ``0.0`` if either side has no
    measurable adversarial composite (nothing to compare; the guardrail
    cannot fire on no data)."""

    baseline_composite = _adversarial_mean_composite(baseline.items, baseline.rows, mode)
    run_composite = _adversarial_mean_composite(run.items, run.rows, mode)
    if baseline_composite is None or run_composite is None:
        return 0.0
    return run_composite - baseline_composite


def judge_error_rate(run: RunArtifact) -> float:
    """Fraction of ATTEMPTED judge calls that errored (spec §7's >5% budget).

    An attempted call is one where ``raw_judge[field] is not None``
    (runner.py's binding convention: ``raw_judge`` is ``None`` for a judged
    field precisely when no call was made at all, i.e. the candidate itself
    failed schema validation/refused). ``0.0`` if no judge calls were ever
    attempted (never a division by zero).
    """

    total = 0
    errors = 0
    for row in run.rows:
        for field_name in JUDGED_FIELDS:
            if row.raw_judge.get(field_name) is None:
                continue
            total += 1
            if row.field_scores[field_name] is None:
                errors += 1
    return errors / total if total else 0.0


# --------------------------------------------------------------------------
# Decision: decide_candidate_result.
# --------------------------------------------------------------------------


def decide_candidate_result(
    label: Literal["a", "b"],
    deltas: Sequence[float],
    *,
    judge_error_excluded: int,
    adversarial_delta: float,
    usage_candidate: Mapping[str, int],
    usage_judge: Mapping[str, int],
    untraced: bool,
    config: Config,
    seed: int = 0,
    n_resamples: int = 10_000,
) -> CandidateGateResult:
    """Applies spec §7's decision rule to already-extracted per-item
    ``deltas`` (nominal slice) plus the already-computed
    ``adversarial_delta``, returning the fully populated
    ``CandidateGateResult`` T10's renderer consumes verbatim.

    Fail iff (one-sided sign-flip p < ``config.gate.alpha`` AND mean
    regression > ``config.gate.margin``) OR the adversarial guardrail trips
    (``adversarial_delta <= -GUARDRAIL_THRESHOLD_POINTS``) -- spec §7: the
    guardrail is a second, independent path to failure, not gated by the
    statistical test.

    Raises ``ValueError`` if ``deltas`` is empty -- every nominal item was
    excluded, so there is nothing to decide on; this should not occur in
    practice (the golden set's nominal slice is fixed and non-trivial) and
    signals a fixture/wiring bug rather than a real gate outcome to handle
    gracefully.
    """

    deltas = list(deltas)
    if not deltas:
        raise ValueError(
            f"candidate {label}: no paired nominal-slice deltas to decide on "
            "(every nominal item excluded -- check judge-error rates)"
        )

    mean_delta = statistics.mean(deltas)
    if len(deltas) >= 2:
        delta_ci = bca_ci(deltas, level=GATE_CI_LEVEL, seed=seed, n_resamples=n_resamples)
        delta_sd = statistics.stdev(deltas)
    else:
        delta_ci = (mean_delta, mean_delta)
        delta_sd = 0.0

    perm = sign_flip_test(deltas, sided="one", seed=seed, n_resamples=n_resamples)
    mde_points = mde(delta_sd, len(deltas), alpha=config.gate.alpha, power=MDE_POWER)

    guardrail_tripped = adversarial_delta <= -GUARDRAIL_THRESHOLD_POINTS
    mean_regression = -mean_delta
    stat_fail = perm.p < config.gate.alpha and mean_regression > config.gate.margin
    verdict: Literal["pass", "fail"] = "fail" if (stat_fail or guardrail_tripped) else "pass"

    return CandidateGateResult(
        label=label,
        verdict=verdict,
        delta=mean_delta,
        delta_ci=delta_ci,
        p_value=perm.p,
        m_nonzero=perm.m_nonzero,
        min_attainable_p=perm.min_attainable_p,
        permutation_method=perm.method,
        mde=mde_points,
        judge_error_excluded=judge_error_excluded,
        adversarial_delta=adversarial_delta,
        adversarial_guardrail_tripped=guardrail_tripped,
        usage_candidate=dict(usage_candidate),
        usage_judge=dict(usage_judge),
        untraced=untraced,
    )


# --------------------------------------------------------------------------
# Orchestration: evaluate_gate.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """The result of a completed (non-erroring) gate evaluation -- either
    pass (0) or a detected regression (1). Measurement errors are raised as
    exceptions (module docstring), never represented here."""

    summary: GateSummaryData
    rendered: str
    exit_code: Literal[0, 1]


def evaluate_gate(
    config: Config,
    *,
    baseline_a: BaselineFile,
    baseline_b: BaselineFile,
    run_a: RunArtifact,
    run_b: RunArtifact,
    certificate: Certificate | None,
    seed_regression: bool = False,
    seed: int = 0,
    n_resamples: int = 10_000,
) -> GateOutcome:
    """Pure orchestration of the full gate decision for both candidates
    (spec §7): fingerprint check (skipped entirely when ``seed_regression``
    -- spec §7's sole exception), judge-error budget check, then the
    per-candidate decision rule, in that order, per candidate. Raises on the
    first problem found (candidate "a" checked before "b").

    ``certificate`` resolves the composite mode (``DETERMINISTIC_5`` iff
    ``certificate.verdict == "inadequate"``, else ``FULL_7``) and the
    calibration verdict fed into the fingerprint comparison; gate runs are
    always reportable (spec §7/§8), so ``render_gate_summary`` raises
    ``MissingCertificateError`` if ``certificate`` is ``None`` (the
    certificate-absent state is not a valid gate state, unlike
    ``--update-baseline``'s deliberately relaxed, explicitly-warned
    "uncalibrated" fallback -- see ``update_baseline``).
    """

    mode = _resolve_composite_mode(certificate)
    calibration_verdict = certificate.verdict if certificate is not None else "uncalibrated"

    results: list[CandidateGateResult] = []
    for label, baseline, run in (("a", baseline_a, run_a), ("b", baseline_b, run_b)):
        if not seed_regression:
            components = fingerprint_components_from_run(
                run, composite_mode=mode, calibration_verdict=calibration_verdict
            )
            mismatches = check_fingerprint(baseline, components)
            if mismatches:
                raise FingerprintMismatchError(label, mismatches)

        rate = judge_error_rate(run)
        if rate > JUDGE_ERROR_RATE_BUDGET:
            raise JudgeErrorBudgetExceededError(label, rate, JUDGE_ERROR_RATE_BUDGET)

        deltas, excluded = nominal_paired_deltas(baseline, run, mode)
        adv_delta = adversarial_composite_delta(baseline, run, mode)

        result = decide_candidate_result(
            label,  # type: ignore[arg-type]
            deltas,
            judge_error_excluded=excluded,
            adversarial_delta=adv_delta,
            usage_candidate=run.usage_totals(),
            usage_judge=run.judge_usage_totals(),
            untraced=run.untraced,
            config=config,
            seed=seed,
            n_resamples=n_resamples,
        )
        results.append(result)

    overall_verdict: Literal["pass", "fail"] = (
        "fail" if any(r.verdict == "fail" for r in results) else "pass"
    )
    data = GateSummaryData(
        certificate=certificate,
        reportable=True,
        composite_mode=mode,
        margin=config.gate.margin,
        alpha=config.gate.alpha,
        k=config.k,
        price_snapshot=config.price_snapshot,
        candidates=tuple(results),
        overall_verdict=overall_verdict,
    )
    rendered = render_gate_summary(data)
    if seed_regression:
        rendered = wrap_demo_mode_banner(rendered)

    return GateOutcome(
        summary=data, rendered=rendered, exit_code=1 if overall_verdict == "fail" else 0
    )


def wrap_demo_mode_banner(rendered: str) -> str:
    """Brackets a ``render_gate_summary`` output with a DEMO MODE banner top
    and bottom (spec §7's ``--seed-regression`` contract). ``GateSummaryData``/
    ``render_gate_summary`` (T10) carry no demo-mode field of their own -- T10's
    contract is a pure formatter over already-decided numbers, and a demo flag
    would be a decision-adjacent concern outside that contract -- so
    ``--seed-regression`` brackets the fully rendered summary here instead, at
    the gate layer, rather than reshaping T10's renderer."""

    return f"{DEMO_MODE_BANNER}\n\n{rendered}\n{DEMO_MODE_BANNER}\n"


# --------------------------------------------------------------------------
# --update-baseline: the one impure entry point in this module.
# --------------------------------------------------------------------------


def update_baseline(
    config: Config,
    model_key: ModelKey,
    *,
    dataset: Sequence[GoldenItem],
    prompt: PromptTemplate = EXTRACTION_PROMPT,
    certificate: Certificate | None,
    runs_root: str | Path = DEFAULT_RUNS_ROOT,
    baselines_root: str | Path = DEFAULT_BASELINES_ROOT,
    trace: TraceContext | None = None,
) -> BaselineFile:
    """Generates a fresh, guardrail-verified baseline for one candidate and
    commits it to ``baselines_root / f"{model_key.label}.json"`` -- refusing
    to touch that path at all if the freshly measured adversarial noise
    floor fails ``check_guardrail_floor`` (spec §7/D3, ``GuardrailFloorError``).

    Uses REAL certificate values (``composite_mode``/``calibration_verdict``)
    when ``certificate`` is supplied. Falls back to the uncalibrated
    placeholder (``FULL_7``/``"uncalibrated"``) with an explicit
    ``warnings.warn`` when it is not -- acceptable ONLY for this
    baseline-generation path (a normal gate run still requires a committed
    certificate via ``render_gate_summary``'s existing
    ``MissingCertificateError`` enforcement; this relaxation is deliberate
    and specific to ``--update-baseline``, per the ticket).

    ``trace`` (additive, keyword-only, default ``None``) is threaded straight
    through to ``generate_baseline``/``run_eval`` -- the caller (``cli.py``)
    is expected to have already validated it via ``TraceContext.for_run(config,
    True)`` (spec §7/§8: baseline generation is a reportable run and must be
    traced) before calling this function.

    Generates into a ``.staging`` subdirectory of ``baselines_root`` first,
    so a failing guardrail check never creates or overwrites the real
    committed baseline file -- "refuses to write" is an observable
    filesystem guarantee, not just a returned error.
    """

    if certificate is not None:
        mode = _resolve_composite_mode(certificate)
        calibration_verdict = certificate.verdict
    else:
        warnings.warn(
            "No calibration certificate found -- generating this baseline with the "
            "uncalibrated placeholder (composite_mode=FULL_7, "
            "calibration_verdict='uncalibrated'). This is acceptable for "
            "--update-baseline only; a normal `eval gate` run still requires a "
            "committed calibration certificate.",
            stacklevel=2,
        )
        mode = CompositeMode.FULL_7
        calibration_verdict = "uncalibrated"

    baselines_root = Path(baselines_root)
    staging_root = baselines_root / ".staging"
    try:
        baseline = generate_baseline(
            config,
            model_key,
            dataset=dataset,
            prompt=prompt,
            composite_mode=mode,
            calibration_verdict=calibration_verdict,
            runs_root=runs_root,
            baselines_root=staging_root,
            trace=trace,
        )

        if not check_guardrail_floor(baseline):
            raise GuardrailFloorError(
                model_key.label,
                baseline.adversarial_noise_floor_se,
                GUARDRAIL_THRESHOLD_POINTS,
                GUARDRAIL_SE_MULTIPLIER,
            )

        final_path = baselines_root / f"{model_key.label}.json"
        staged_path = staging_root / f"{model_key.label}.json"
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_text(staged_path.read_text(encoding="utf-8"), encoding="utf-8")
        return baseline
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


__all__ = [
    "DEMO_MODE_BANNER",
    "GATE_CI_LEVEL",
    "JUDGE_ERROR_RATE_BUDGET",
    "MDE_POWER",
    "FingerprintMismatchError",
    "GateOutcome",
    "GuardrailFloorError",
    "JudgeErrorBudgetExceededError",
    "MissingBaselineError",
    "adversarial_composite_delta",
    "decide_candidate_result",
    "evaluate_gate",
    "judge_error_rate",
    "nominal_paired_deltas",
    "update_baseline",
    "wrap_demo_mode_banner",
]
