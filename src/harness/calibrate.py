"""Judge calibration: agreement report, certificate, self-consistency (spec
§5, §4; ticket T14).

**Candidate-outputs-for-calibration design (binding, read before touching
this module):** this module does NOT invent a parallel pipeline for
generating candidate outputs over the calibration emails. Instead it reuses
T08's ``run_eval``/``load_run`` machinery exactly the way ``eval run``/
``eval compare`` already do: the calibration procedure runs each candidate
over ``data/calibration/emails.jsonl`` via the ordinary ``run_eval`` path
(``cli.py``'s ``calibrate`` command drives this through the same
``_get_or_run`` seam ``run``/``compare`` use), producing one persisted
``RunArtifact`` per candidate. ``build_triples`` then *reconstructs* the
``(email, reference, candidate_value)`` triples this module needs straight
from that artifact's own persisted rows: ``email``/``expected`` come from the
artifact's embedded ``GoldenItem``s (persisted verbatim at run time, spec
AC5), and ``candidate_value`` comes from parsing that item's persisted
``raw_output`` JSON. No candidate call is ever made here -- only the JUDGE is
re-invoked (``judge_triples``, ``measure_self_consistency``), against
already-produced candidate output, which is exactly what "the judge is run
over the (email, reference, candidate_value) triples reconstructed from
persisted candidate outputs" means operationally.

Calibration runs are expected at K=1 (spec §5's 25 x 2 fields x 2 candidates
= 100 field judgments -- one candidate output per item per field, not K
replicates of it; ``CalibrationLabel`` itself carries no replicate index).
``build_triples`` tolerates K>1 defensively by always taking the
lowest-``replicate`` row per item, but production calibration runs should be
generated with ``k=1``.

Re-judging (rather than trusting the candidate run's own embedded
``field_scores``, which ``run_eval`` already computed via ``Judge.judge_field``
at run time) is deliberate: calibration must certify the CURRENT judge
(``judge_version()``), and re-invoking it here -- through the exact same
``Judge.judge_field`` call every production judged field goes through -- is
what lets this module assert its own ``judge_version()`` on the certificate
without trusting that the candidate run's manifest was produced under an
identical judge (a run and a calibration certification are not guaranteed to
happen atomically with each other).

**Statistics (spec §5, D2):** Cohen's kappa (``harness.stats.agreement.
cohens_kappa``) is the single agreement statistic, computed overall and per
candidate, always with a cluster-bootstrap CI resampling emails (judgments of
one email -- both candidates, both fields -- move together). Raw agreement
and label prevalence are carried through as descriptive context only, never
a decision input. Adequacy is decided on the OVERALL kappa point estimate
(``decide_verdict``); a per-candidate kappa gap above 0.2 is a flag for D1
review only (``per_candidate_divergence_flag``), never a gate condition.

**Self-consistency (spec §4):** 20 fixed ``(email, reference,
candidate-value)`` triples -- a deterministic prefix of the full,
sorted-by-``(item_id, candidate, field)`` triple set, so the same calibration
data always selects the same 20 -- are each judged 3x; the flip rate (the
fraction of the 20 for which the 3 judgments disagree) is reported and
carried into the certificate context.

**Test-retest ceiling (spec §5, ``--retest``):** intra-annotator kappa on the
label-id intersection of ``round="initial"`` and ``round="retest"`` labels,
with its own cluster-bootstrap CI, surfaced as ``ceiling_kappa`` on the
certificate and explicitly labeled *an estimate of the consistency ceiling*
in the rendered report -- a judge kappa exceeding it is estimation noise, not
a super-human judge (never treated as a red flag).

**Bootstrap omission disclosure (D2/agreement.py):** ``cohens_kappa``'s CI
path may emit exactly one ``RuntimeWarning`` disclosing how many degenerate
bootstrap resamples were omitted (see ``stats/agreement.py``'s module
docstring). Every kappa call in this module is wrapped to capture such
warnings via ``warnings.catch_warnings(record=True)`` rather than letting
them propagate to the caller's default warning handling, and the captured
messages are rendered verbatim in the calibration report's "Bootstrap
Disclosures" section -- silently swallowing them would hide exactly the
disclosure spec/D2 requires.

**Certificate (schema.py ``Certificate``, committed as
``data/calibration/certificate.json``):** ``build_certificate`` populates
every spec §5 field, including the additive ``per_candidate_kappa_ci`` (T14
closing a ledgered T01/T10 gap -- see ``schema.py``'s ``Certificate``
docstring). ``date`` defaults to the most recent ``label_date`` across all
labels (any round) unless an explicit override is supplied -- never
wall-clock-dependent, so tests and re-runs against the same label file always
produce the same certificate date.
"""

from __future__ import annotations

import hashlib
import json
import math
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from harness.judge.judge import Judge
from harness.judge.rubric import judge_version as compute_judge_version
from harness.runner import RunArtifact
from harness.schema import CalibrationLabel, Certificate, EmailInput, GoldenItem
from harness.scoring.composite import JUDGED_FIELDS
from harness.stats.agreement import KappaResult, cohens_kappa

# Spec §5 pinned parameters -- changing these needs a dated decision-log amendment.
ADEQUACY_KAPPA_THRESHOLD = 0.6
GRAY_ZONE_CI_LOWER_THRESHOLD = 0.4
DIVERGENCE_GAP_THRESHOLD = 0.2
STRATIFICATION_FAIL_RATE_THRESHOLD = 0.20

# Spec §4: 20 fixed triples, judged 3x each.
DEFAULT_SELF_CONSISTENCY_N = 20
DEFAULT_SELF_CONSISTENCY_REPEATS = 3

DEFAULT_CI_LEVEL = 0.95
DEFAULT_N_RESAMPLES = 10_000


# --------------------------------------------------------------------------
# Triple reconstruction (module docstring's candidate-outputs design).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Triple:
    """One ``(email, reference, candidate_value)`` triple for one judged
    field of one candidate's output on one calibration item -- reconstructed
    from a persisted ``RunArtifact`` (module docstring), never freshly
    generated by this module."""

    item_id: str
    candidate: Literal["a", "b"]
    field: str
    email: EmailInput
    reference: str
    candidate_value: str


def build_triples(candidate: Literal["a", "b"], run_artifact: RunArtifact) -> list[Triple]:
    """Reconstructs every judgeable ``Triple`` for ``candidate`` from
    ``run_artifact``'s persisted items/rows, in deterministic ``item_id``
    order.

    Uses the lowest-``replicate`` row per item (calibration runs are expected
    at K=1 -- module docstring); an item whose candidate row is missing, or
    whose ``raw_output`` does not parse as a JSON object carrying the judged
    field (a schema-invalid/refusal candidate failure, which never gets a
    judge call in ``run_eval`` either), contributes no triples for that item.
    """

    triples: list[Triple] = []
    for item in sorted(run_artifact.items, key=lambda i: i.id):
        rows = run_artifact.rows_for_item(item.id)
        if not rows:
            continue
        row = min(rows, key=lambda r: r.replicate)
        try:
            output = json.loads(row.raw_output)
        except json.JSONDecodeError:
            continue
        if not isinstance(output, dict):
            continue
        for field in JUDGED_FIELDS:
            if field not in output:
                continue
            reference = str(getattr(item.expected, field))
            candidate_value = str(output[field])
            triples.append(
                Triple(
                    item_id=item.id,
                    candidate=candidate,
                    field=field,
                    email=item.email,
                    reference=reference,
                    candidate_value=candidate_value,
                )
            )
    return triples


# --------------------------------------------------------------------------
# Judging triples (re-invokes the current judge -- module docstring).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgedTriple:
    """One ``Triple`` plus the current judge's fresh verdict on it. ``verdict
    is None`` iff ``error`` is set (``Judge.judge_field``'s own error-vs-fail
    convention, spec §7: a judge error is never coerced to ``"fail"``)."""

    triple: Triple
    verdict: Literal["pass", "fail"] | None
    error: str | None
    rationale: str | None


def judge_triples(judge: Judge, triples: Sequence[Triple]) -> list[JudgedTriple]:
    """Judges every triple exactly once via ``Judge.judge_field``, preserving
    input order."""

    judged: list[JudgedTriple] = []
    for t in triples:
        result = judge.judge_field(t.email, t.field, t.reference, t.candidate_value)
        judged.append(
            JudgedTriple(
                triple=t, verdict=result.verdict, error=result.error, rationale=result.rationale
            )
        )
    return judged


# --------------------------------------------------------------------------
# Pairing judged triples with owner labels.
# --------------------------------------------------------------------------


def _labels_by_key(
    labels: Sequence[CalibrationLabel], round_: Literal["initial", "retest"]
) -> dict[tuple[str, str, str], Literal["pass", "fail"]]:
    """``{(item_id, candidate, field): verdict}`` for one label round. Raises
    ``ValueError`` on a duplicate key within the same round -- a data
    integrity problem (two labels for the same judged field), never silently
    resolved by picking one."""

    by_key: dict[tuple[str, str, str], Literal["pass", "fail"]] = {}
    for label in labels:
        if label.round != round_:
            continue
        key = (label.item_id, label.candidate, label.field)
        if key in by_key:
            raise ValueError(f"duplicate {round_!r} label for {key} in labels.jsonl")
        by_key[key] = label.verdict
    return by_key


@dataclass(frozen=True)
class PairedJudgment:
    """One (owner label, judge verdict) pair -- both determinate -- ready to
    feed ``cohens_kappa``."""

    item_id: str
    candidate: Literal["a", "b"]
    owner_verdict: Literal["pass", "fail"]
    judge_verdict: Literal["pass", "fail"]


def pair_with_labels(
    judged: Sequence[JudgedTriple],
    labels: Sequence[CalibrationLabel],
    *,
    round_: Literal["initial", "retest"] = "initial",
) -> tuple[list[PairedJudgment], int, int]:
    """Joins judged triples to owner labels for ``round_``.

    Returns ``(paired, judge_errors_excluded, unlabeled_excluded)``: a judge
    error (``verdict is None``) is excluded, never coerced to ``"fail"``
    (spec §7); a judged triple with no matching label is excluded and counted
    separately (e.g. a stratification-loop addition not yet labeled). Both
    exclusion counts are disclosed in the report rather than silently
    dropped.
    """

    by_key = _labels_by_key(labels, round_)
    paired: list[PairedJudgment] = []
    judge_errors = 0
    unlabeled = 0
    for jt in judged:
        key = (jt.triple.item_id, jt.triple.candidate, jt.triple.field)
        owner_verdict = by_key.get(key)
        if owner_verdict is None:
            unlabeled += 1
            continue
        if jt.verdict is None:
            judge_errors += 1
            continue
        paired.append(
            PairedJudgment(
                item_id=jt.triple.item_id,
                candidate=jt.triple.candidate,
                owner_verdict=owner_verdict,
                judge_verdict=jt.verdict,
            )
        )
    return paired, judge_errors, unlabeled


# --------------------------------------------------------------------------
# Agreement statistics.
# --------------------------------------------------------------------------


def _kappa_with_capture(
    a: Sequence[str],
    b: Sequence[str],
    clusters: Sequence[str],
    *,
    ci_level: float,
    n_resamples: int,
    seed: int,
) -> tuple[KappaResult, list[str]]:
    """Calls ``cohens_kappa`` capturing any ``RuntimeWarning`` it emits
    (the degenerate-resample omission disclosure, D2/agreement.py) instead of
    letting it propagate to the caller's default warning handling -- the
    disclosure is rendered explicitly in the calibration report instead."""

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        result = cohens_kappa(
            a, b, clusters=clusters, level=ci_level, n_resamples=n_resamples, seed=seed
        )
    messages = [str(w.message) for w in caught if issubclass(w.category, RuntimeWarning)]
    return result, messages


def compute_agreement(
    paired: Sequence[PairedJudgment],
    *,
    ci_level: float = DEFAULT_CI_LEVEL,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    seed: int = 0,
) -> tuple[KappaResult, dict[str, KappaResult], tuple[str, ...]]:
    """Overall and per-candidate Cohen's kappa (with cluster-bootstrap CIs,
    clusters = email/``item_id``) over ``paired`` judgments -- spec §5, D2.

    Returns ``(overall, per_candidate, warnings)``: ``per_candidate`` is keyed
    by whichever candidate labels appear in ``paired`` (normally ``"a"``,
    ``"b"``); ``warnings`` pools every captured bootstrap-omission disclosure
    from the overall and per-candidate calls, in call order.
    """

    def _subset_kappa(subset: Sequence[PairedJudgment]) -> tuple[KappaResult, list[str]]:
        a = [p.owner_verdict for p in subset]
        b = [p.judge_verdict for p in subset]
        clusters = [p.item_id for p in subset]
        return _kappa_with_capture(
            a, b, clusters, ci_level=ci_level, n_resamples=n_resamples, seed=seed
        )

    overall, overall_warnings = _subset_kappa(paired)

    per_candidate: dict[str, KappaResult] = {}
    all_warnings = list(overall_warnings)
    for label in sorted({p.candidate for p in paired}):
        subset = [p for p in paired if p.candidate == label]
        result, msgs = _subset_kappa(subset)
        per_candidate[label] = result
        all_warnings.extend(msgs)

    return overall, per_candidate, tuple(all_warnings)


def decide_verdict(
    kappa: float, ci: tuple[float, float]
) -> Literal["adequate", "adequate_with_caveat", "inadequate"]:
    """Spec §5's adequacy policy, decided on the overall kappa POINT ESTIMATE:
    ``kappa >= 0.6`` with CI lower bound ``>= 0.4`` -> ``"adequate"``;
    ``kappa >= 0.6`` with CI lower bound ``< 0.4`` (gray zone) ->
    ``"adequate_with_caveat"``; otherwise ``"inadequate"``."""

    if kappa >= ADEQUACY_KAPPA_THRESHOLD:
        if ci[0] < GRAY_ZONE_CI_LOWER_THRESHOLD:
            return "adequate_with_caveat"
        return "adequate"
    return "inadequate"


def per_candidate_divergence_flag(per_candidate_kappa: Mapping[str, float]) -> bool:
    """``True`` iff the spread between per-candidate kappa point estimates
    exceeds 0.2 (spec §5/D1: a D1-review FLAG, never a gate condition). NaN
    kappas (degenerate single-category subsets) are excluded from the
    comparison -- fewer than two comparable values means no gap can be
    computed, so this returns ``False`` rather than raising."""

    values = [v for v in per_candidate_kappa.values() if not math.isnan(v)]
    if len(values) < 2:
        return False
    return (max(values) - min(values)) > DIVERGENCE_GAP_THRESHOLD


# --------------------------------------------------------------------------
# Self-consistency (spec §4).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SelfConsistencyResult:
    n_triples: int
    repeats: int
    flip_rate: float
    flipped_triples: tuple[tuple[str, str, str], ...]  # (item_id, candidate, field)


def select_fixed_self_consistency_triples(
    triples: Sequence[Triple], n: int = DEFAULT_SELF_CONSISTENCY_N
) -> list[Triple]:
    """A deterministic prefix of ``triples``, sorted by ``(item_id,
    candidate, field)`` -- the "20 fixed triples" spec §4 requires: the same
    calibration data always selects the same subset. Returns fewer than ``n``
    (never raises) if fewer triples are available -- only expected to matter
    for small test fixtures; real calibration data has 100 available."""

    ordered = sorted(triples, key=lambda t: (t.item_id, t.candidate, t.field))
    return ordered[:n]


def measure_self_consistency(
    judge: Judge, triples: Sequence[Triple], *, repeats: int = DEFAULT_SELF_CONSISTENCY_REPEATS
) -> SelfConsistencyResult:
    """Judges each of ``triples`` ``repeats`` times (spec §4: 3x), reporting
    the flip rate -- the fraction of triples whose repeated verdicts are not
    unanimous. A triple with fewer than 2 determinate (non-error) verdicts
    among its repeats cannot show a flip and is never counted as one."""

    flipped: list[tuple[str, str, str]] = []
    for t in triples:
        verdicts = [
            judge.judge_field(t.email, t.field, t.reference, t.candidate_value).verdict
            for _ in range(repeats)
        ]
        determinate = [v for v in verdicts if v is not None]
        if len(determinate) >= 2 and len(set(determinate)) > 1:
            flipped.append((t.item_id, t.candidate, t.field))

    n = len(triples)
    flip_rate = len(flipped) / n if n else 0.0
    return SelfConsistencyResult(
        n_triples=n, repeats=repeats, flip_rate=flip_rate, flipped_triples=tuple(flipped)
    )


# --------------------------------------------------------------------------
# Test-retest ceiling (spec §5, --retest).
# --------------------------------------------------------------------------


def compute_retest_ceiling(
    labels: Sequence[CalibrationLabel],
    *,
    ci_level: float = DEFAULT_CI_LEVEL,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    seed: int = 0,
) -> tuple[KappaResult | None, tuple[str, ...]]:
    """Intra-annotator Cohen's kappa (with its own cluster-bootstrap CI) on
    the ``(item_id, candidate, field)`` intersection of ``round="initial"``
    and ``round="retest"`` labels -- spec §5's "estimate of the consistency
    ceiling". Returns ``(None, ())`` if fewer than 2 keys are shared (no
    meaningful ceiling to compute; e.g. no retest labels at all).
    """

    initial = _labels_by_key(labels, "initial")
    retest = _labels_by_key(labels, "retest")
    shared_keys = sorted(set(initial) & set(retest))
    if len(shared_keys) < 2:
        return None, ()

    a = [initial[key] for key in shared_keys]
    b = [retest[key] for key in shared_keys]
    clusters = [key[0] for key in shared_keys]  # item_id (email)
    result, messages = _kappa_with_capture(
        a, b, clusters, ci_level=ci_level, n_resamples=n_resamples, seed=seed
    )
    return result, tuple(messages)


# --------------------------------------------------------------------------
# Disjointness from the golden set (spec §5 AC).
# --------------------------------------------------------------------------


def check_disjoint_from_golden(
    calibration_items: Sequence[GoldenItem], golden_items: Sequence[GoldenItem]
) -> None:
    """Raises ``ValueError`` if any calibration item shares an id, or exact
    (subject, body) email content, with a golden item (spec §5: the
    calibration set must be disjoint from golden). Names every offending id
    so a violation is immediately actionable."""

    golden_ids = {item.id for item in golden_items}
    golden_emails = {(item.email.subject, item.email.body) for item in golden_items}

    id_overlap = sorted({item.id for item in calibration_items} & golden_ids)
    email_overlap = sorted(
        item.id
        for item in calibration_items
        if (item.email.subject, item.email.body) in golden_emails
    )
    if id_overlap or email_overlap:
        raise ValueError(
            "calibration items overlap with the golden set (spec §5, must be disjoint): "
            f"id overlap={id_overlap}, email-content overlap (calibration ids)={email_overlap}"
        )


# --------------------------------------------------------------------------
# Orchestration: run_calibration, build_certificate, render_calibration_report.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationResult:
    """Everything ``build_certificate``/``render_calibration_report`` need,
    already computed by ``run_calibration``."""

    judge_version: str
    label_file_hash: str
    date: date
    overall: KappaResult
    per_candidate: dict[str, KappaResult]
    verdict: Literal["adequate", "adequate_with_caveat", "inadequate"]
    divergence_flag: bool
    initial_fail_rate: float
    fail_enrichment_note: bool
    judge_errors_excluded: int
    unlabeled_excluded: int
    self_consistency: SelfConsistencyResult
    ceiling: KappaResult | None
    warnings: tuple[str, ...]


def run_calibration(
    *,
    run_a: RunArtifact,
    run_b: RunArtifact,
    labels: Sequence[CalibrationLabel],
    judge: Judge,
    label_file_hash: str,
    date_override: date | None = None,
    self_consistency_n: int = DEFAULT_SELF_CONSISTENCY_N,
    self_consistency_repeats: int = DEFAULT_SELF_CONSISTENCY_REPEATS,
    ci_level: float = DEFAULT_CI_LEVEL,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    seed: int = 0,
    retest: bool = False,
) -> CalibrationResult:
    """The full judge-calibration measurement (spec §5, §4): reconstructs
    triples from both candidates' persisted calibration runs, re-judges them
    with the current judge, computes overall/per-candidate agreement against
    owner labels, the adequacy verdict, the per-candidate divergence flag,
    the stratification fail-rate note, judge self-consistency, and (when
    ``retest``) the test-retest consistency ceiling.

    Raises ``ValueError`` if no judged triple has a matching ``round="initial"``
    label at all -- there is nothing to compute agreement on, which signals a
    wiring problem (mismatched dataset/label files) rather than a real
    calibration outcome to report on.
    """

    triples_a = build_triples("a", run_a)
    triples_b = build_triples("b", run_b)
    all_triples = triples_a + triples_b

    judged = judge_triples(judge, all_triples)
    paired, judge_errors_excluded, unlabeled_excluded = pair_with_labels(
        judged, labels, round_="initial"
    )
    if not paired:
        raise ValueError(
            "no labeled triple matched a judged calibration output -- check that "
            "labels.jsonl and the calibration runs describe the same items"
        )

    overall, per_candidate, agreement_warnings = compute_agreement(
        paired, ci_level=ci_level, n_resamples=n_resamples, seed=seed
    )
    verdict = decide_verdict(overall.kappa, overall.ci)
    divergence = per_candidate_divergence_flag(
        {label: result.kappa for label, result in per_candidate.items()}
    )

    initial_labels = [label for label in labels if label.round == "initial"]
    fail_count = sum(1 for label in initial_labels if label.verdict == "fail")
    initial_fail_rate = fail_count / len(initial_labels) if initial_labels else 0.0
    fail_enrichment_note = initial_fail_rate < STRATIFICATION_FAIL_RATE_THRESHOLD

    fixed_triples = select_fixed_self_consistency_triples(all_triples, self_consistency_n)
    self_consistency = measure_self_consistency(
        judge, fixed_triples, repeats=self_consistency_repeats
    )

    ceiling: KappaResult | None = None
    ceiling_warnings: tuple[str, ...] = ()
    if retest:
        ceiling, ceiling_warnings = compute_retest_ceiling(
            labels, ci_level=ci_level, n_resamples=n_resamples, seed=seed
        )

    resolved_date = resolve_certificate_date(labels, date_override)

    return CalibrationResult(
        judge_version=compute_judge_version(),
        label_file_hash=label_file_hash,
        date=resolved_date,
        overall=overall,
        per_candidate=per_candidate,
        verdict=verdict,
        divergence_flag=divergence,
        initial_fail_rate=initial_fail_rate,
        fail_enrichment_note=fail_enrichment_note,
        judge_errors_excluded=judge_errors_excluded,
        unlabeled_excluded=unlabeled_excluded,
        self_consistency=self_consistency,
        ceiling=ceiling,
        warnings=agreement_warnings + ceiling_warnings,
    )


def build_certificate(result: CalibrationResult) -> Certificate:
    """Builds the committed ``Certificate`` (spec §5) from an already-computed
    ``CalibrationResult`` -- every field named in spec §5, including the
    additive ``per_candidate_kappa_ci`` (T14)."""

    return Certificate(
        judge_version=result.judge_version,
        overall_kappa=result.overall.kappa,
        kappa_ci=result.overall.ci,
        per_candidate_kappa={label: r.kappa for label, r in result.per_candidate.items()},
        per_candidate_kappa_ci={label: r.ci for label, r in result.per_candidate.items()},
        verdict=result.verdict,
        ceiling_kappa=result.ceiling.kappa if result.ceiling is not None else None,
        label_file_hash=result.label_file_hash,
        date=result.date,
    )


def render_calibration_report(result: CalibrationResult) -> str:
    """Renders the ``eval calibrate`` markdown report: agreement (overall +
    per-candidate, with CIs, raw agreement, prevalence as descriptive
    context), the adequacy verdict, the D1-review divergence flag when it
    fires, the stratification fail-rate note, judge self-consistency, the
    test-retest ceiling row (when computed), and any bootstrap-omission
    disclosures."""

    lines: list[str] = ["# Judge Calibration Report", ""]
    lines.append(f"- Judge version: `{result.judge_version}`")
    lines.append(f"- Certificate date: {result.date.isoformat()}")
    lines.append(f"- Label file hash: `{result.label_file_hash}`")
    lines.append("")

    lines.append("## Agreement")
    lines.append("")
    lines.append(
        f"- Overall Cohen's κ = {result.overall.kappa:.3f} (95% cluster-bootstrap CI "
        f"[{result.overall.ci[0]:.3f}, {result.overall.ci[1]:.3f}])"
    )
    lines.append(
        f"- Raw agreement (descriptive context only): {result.overall.raw_agreement:.1%}"
    )
    lines.append(
        f"- Label prevalence, pass (descriptive context only): {result.overall.prevalence:.1%}"
    )
    lines.append(f"- Verdict: **{result.verdict}**")
    if result.verdict == "adequate_with_caveat":
        lines.append(
            "  - Gray zone: κ̂ >= 0.6 but the CI lower bound < 0.4 (spec §5) -- flagged, not gated."
        )
    lines.append("")

    lines.append("### Per-Candidate")
    lines.append("")
    for label in sorted(result.per_candidate):
        r = result.per_candidate[label]
        lines.append(
            f"- candidate {label}: κ = {r.kappa:.3f} (95% CI [{r.ci[0]:.3f}, {r.ci[1]:.3f}]), "
            f"raw agreement {r.raw_agreement:.1%}, prevalence {r.prevalence:.1%}"
        )
    lines.append("")
    if result.divergence_flag:
        lines.append(
            "> **D1-review flag:** per-candidate κ differs by more than 0.2 -- differential "
            "judge error across candidates (spec §5/D1). A flag for review, never a gate "
            "condition."
        )
        lines.append("")

    lines.append(
        f"Excluded from agreement: {result.judge_errors_excluded} judge error(s) (never "
        f"counted as fail, spec §7), {result.unlabeled_excluded} judged field(s) with no "
        "matching label."
    )
    lines.append("")

    lines.append("## Stratification")
    lines.append("")
    lines.append(f"Initial fail-label rate: {result.initial_fail_rate:.1%}.")
    if result.fail_enrichment_note:
        lines.append("")
        lines.append(
            "> Below the 20% stratification-loop threshold (spec §5): the calibration set "
            "was enriched with harder-category emails after the initial round. Agreement "
            "above is measured on a harder-than-operational distribution -- a conservative "
            "estimate, not an inflated one."
        )
    lines.append("")

    lines.append("## Judge Self-Consistency")
    lines.append("")
    lines.append(
        f"{result.self_consistency.n_triples} fixed (email, reference, candidate-value) "
        f"triple(s), each judged {result.self_consistency.repeats}x: flip rate = "
        f"{result.self_consistency.flip_rate:.1%} "
        f"({len(result.self_consistency.flipped_triples)}/{result.self_consistency.n_triples})."
    )
    lines.append("")

    if result.ceiling is not None:
        lines.append("## Test-Retest Consistency Ceiling")
        lines.append("")
        lines.append(
            f"Intra-annotator κ on the initial/retest intersection = "
            f"{result.ceiling.kappa:.3f} (95% cluster-bootstrap CI "
            f"[{result.ceiling.ci[0]:.3f}, {result.ceiling.ci[1]:.3f}]) -- an estimate of "
            "the consistency ceiling. A judge κ exceeding this value indicates estimation "
            "noise, not a super-human judge."
        )
        lines.append("")

    if result.warnings:
        lines.append("## Bootstrap Disclosures")
        lines.append("")
        for message in result.warnings:
            lines.append(f"- {message}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------
# Small I/O helpers (the CLI composes these; kept here so calibrate.py owns
# its own file format, mirroring gate/baseline.py's mix of pure logic +
# small, explicit I/O helpers).
# --------------------------------------------------------------------------


def load_calibration_labels(path: str | Path) -> list[CalibrationLabel]:
    """Parses ``data/calibration/labels.jsonl`` into ``CalibrationLabel``s."""

    labels: list[CalibrationLabel] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            labels.append(CalibrationLabel.model_validate(json.loads(line)))
    return labels


def hash_label_file(path: str | Path) -> str:
    """sha256 of the raw bytes of ``labels.jsonl`` -- ``Certificate.
    label_file_hash`` (spec §5): ties a certificate to the exact label
    content that produced it."""

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def resolve_certificate_date(
    labels: Sequence[CalibrationLabel], explicit: date | None = None
) -> date:
    """``explicit`` when given; otherwise the most recent ``label_date``
    across ALL labels (any round) -- never wall-clock-dependent (spec/ticket:
    the certificate date must be reproducible from the label file alone).
    Raises ``ValueError`` if neither is available."""

    if explicit is not None:
        return explicit
    if not labels:
        raise ValueError("cannot resolve a certificate date: no labels and no explicit date given")
    return max(label.label_date for label in labels)


def write_certificate(certificate: Certificate, path: str | Path) -> None:
    """Writes ``certificate`` to ``path`` (``data/calibration/certificate.json``),
    creating parent directories as needed."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(certificate.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
    )


__all__ = [
    "ADEQUACY_KAPPA_THRESHOLD",
    "DEFAULT_SELF_CONSISTENCY_N",
    "DEFAULT_SELF_CONSISTENCY_REPEATS",
    "DIVERGENCE_GAP_THRESHOLD",
    "GRAY_ZONE_CI_LOWER_THRESHOLD",
    "STRATIFICATION_FAIL_RATE_THRESHOLD",
    "CalibrationResult",
    "JudgedTriple",
    "PairedJudgment",
    "SelfConsistencyResult",
    "Triple",
    "build_certificate",
    "build_triples",
    "check_disjoint_from_golden",
    "compute_agreement",
    "compute_retest_ceiling",
    "decide_verdict",
    "hash_label_file",
    "judge_triples",
    "load_calibration_labels",
    "measure_self_consistency",
    "pair_with_labels",
    "per_candidate_divergence_flag",
    "render_calibration_report",
    "resolve_certificate_date",
    "run_calibration",
    "select_fixed_self_consistency_triples",
    "write_certificate",
]
