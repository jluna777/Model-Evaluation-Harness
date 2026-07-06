"""Markdown report rendering: certificate/untraced banners + the three
renderers (spec §5, §6, §7).

**Purity contract (binding for T11's ``rescore`` and spec AC5):** every
function here is a pure function over already-persisted data --
``RunArtifact`` (T08), a ``Certificate`` (T01), or a ``GateSummaryData``
(this module). No client construction, no API calls, no filesystem access
beyond what the caller already handed in. This is what lets ``eval rescore``
regenerate a byte-identical report from a run directory with zero API calls,
and what lets the gate module (T16) compute its decision once and hand the
same numbers to both the exit-code logic and this module's renderer.

**Certificate header policy (spec §5, applies to every report):**

- A certificate present -> render judge version, overall kappa +/- its CI,
  verdict, and a per-candidate kappa breakdown. ``Certificate`` (schema.py)
  carries ``kappa_ci`` for the *overall* kappa only -- there is no
  per-candidate CI field on the committed certificate, despite spec §5's
  prose ("Per-candidate kappas are reported with CIs"). This module renders
  what the schema actually carries (per-candidate kappa *point estimates*,
  the overall CI) rather than inventing a per-candidate CI with no backing
  data; the gap is noted inline in the rendered header so a reader isn't
  misled into thinking one figure is the other.
- A certificate absent, ``reportable=False`` -> render the dev-stage
  "UNCALIBRATED (no certificate)" banner. This is the allowed state for dev
  iteration (spec §8).
- A certificate absent, ``reportable=True`` -> raise ``MissingCertificateError``.
  A reportable run (baseline updates, README numbers, gate runs, spec §8) is
  refused outright rather than silently degraded to the dev-stage banner.
- ``certificate.verdict == "inadequate"`` -> judged fields are excluded from
  every composite figure in the report (``CompositeMode.DETERMINISTIC_5``,
  spec §5 degraded mode), flagged explicitly in the header.
  ``"adequate_with_caveat"`` keeps ``FULL_7`` but adds a caveat line.

**Composite-mode resolution:** ``_resolve_composite_mode`` derives the mode
purely from the certificate (never from the run artifact's manifest, whose
``composite_mode`` field is a T08 run-identity placeholder, not a report-time
decision -- see ``runner.py``'s module docstring). ``render_gate_summary`` is
the one exception: it trusts ``GateSummaryData.composite_mode`` verbatim,
since the gate module (T16) is the one that actually computed every delta
under that mode and must remain the single source of truth for which mode
was used, even though it would normally resolve to the same value.

**Missing-judge-verdict convention for composite scores:** a row's
``field_scores[field] is None`` means "judged, but the judge call errored"
(runner.py's binding convention) -- never a 0. This module's row-level
composite (``_row_composite``) therefore *excludes* ``None`` fields from that
row's average rather than zeroing them: the average is taken over whichever
of the mode's fields have a determinate 0/1 score. A row where every
mode-included field is missing (both judged fields errored, only possible
under ``FULL_7``) has an undefined composite (``None``) and is excluded from
whatever aggregate is being computed, never silently coerced to 0.

**Delta sign convention (compare + gate):** paired per-item deltas are
``current - baseline`` (or, for ``eval compare``, ``candidate_b -
candidate_a``) -- matching ``harness.stats.permutation``'s own docstring
convention. A *negative* delta is a regression; the gate's one-sided test
(spec §7) tests exactly this direction.

**``docs/gate-design.md`` link convention:** the link this module emits is
root-relative (``docs/gate-design.md``, no leading ``./`` or ``/``) rather
than relative to wherever the gate summary markdown eventually gets written
(a job summary / PR comment has no filesystem location of its own to be
relative *to*). T17's acceptance criterion resolves it "from the repo root",
which is exactly what this convention assumes.

**Reproducibility:** every renderer that runs a bootstrap or permutation
test (``render_run_report``, ``render_compare_report``) takes an explicit
``seed`` and ``n_resamples`` (defaults matching the stats modules' own
defaults) so a given call always produces byte-identical output --
required for the golden-file tests and for ``rescore``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from harness.config import ModelPrice, PriceSnapshot
from harness.runner import ALL_FIELDS, RunArtifact, RunRow
from harness.schema import Certificate
from harness.scoring.composite import DETERMINISTIC_FIELDS, JUDGED_FIELDS, CompositeMode
from harness.stats.bootstrap import bca_ci
from harness.stats.permutation import sign_flip_test
from harness.stats.variance import variance_components

GATE_DESIGN_DOC_LINK = "docs/gate-design.md"

# Mirrors scoring.composite's own private `_MODE_FIELDS` mapping, built from
# the same public field-group constants rather than reaching into that
# module's private name -- see module docstring re: composite-mode
# resolution. Kept in sync by construction: any change to
# DETERMINISTIC_FIELDS/JUDGED_FIELDS in composite.py is picked up here too.
_MODE_FIELDS: dict[CompositeMode, tuple[str, ...]] = {
    CompositeMode.FULL_7: DETERMINISTIC_FIELDS + JUDGED_FIELDS,
    CompositeMode.DETERMINISTIC_5: DETERMINISTIC_FIELDS,
}

_RUN_REPORT_CI_LEVEL = 0.95
_COMPARE_REPORT_CI_LEVEL = 0.95


class MissingCertificateError(Exception):
    """Raised when a REPORTABLE report (spec §8) is rendered with no
    calibration certificate. Reportable runs -- baseline updates,
    README/published numbers, calibration certification, and gate runs --
    require a committed certificate and are refused outright; the
    "uncalibrated" banner exists specifically for non-reportable dev
    iteration, and is not a fallback for a reportable run missing one."""


# --------------------------------------------------------------------------
# Small pure helpers shared by all three renderers.
# --------------------------------------------------------------------------


def _pearson_r(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Simple Pearson correlation coefficient over paired ``xs``/``ys``.

    Returns ``None`` (rather than raising or returning NaN) when fewer than
    2 pairs are given, or either series has zero variance -- both make the
    correlation coefficient undefined, and a report should say "n/a", not
    crash or print ``nan``.
    """

    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0.0 or var_y == 0.0:
        return None
    return cov / math.sqrt(var_x * var_y)


def _resolve_composite_mode(certificate: Certificate | None) -> CompositeMode:
    """The composite definition every aggregate figure in a report uses
    (spec §5's degraded mode): ``DETERMINISTIC_5`` iff the certificate's
    verdict is ``"inadequate"``; ``FULL_7`` otherwise -- including when
    there is no certificate at all (a dev-stage/uncalibrated report is not
    automatically judge-excluded; only a certified ``inadequate`` verdict
    triggers exclusion)."""

    if certificate is not None and certificate.verdict == "inadequate":
        return CompositeMode.DETERMINISTIC_5
    return CompositeMode.FULL_7


def _row_composite(field_scores: Mapping[str, int | None], mode: CompositeMode) -> float | None:
    """Per-row composite (0-100) over ``mode``'s included fields, excluding
    (never zeroing) a missing judge verdict -- see module docstring's
    missing-judge-verdict convention. ``None`` iff every included field is
    missing for this row."""

    values = [field_scores[f] for f in _MODE_FIELDS[mode] if field_scores[f] is not None]
    if not values:
        return None
    return sum(values) / len(values) * 100.0


def _judged_only_composite(field_scores: Mapping[str, int | None]) -> float | None:
    """Per-row composite (0-100) over the two judged fields only, regardless
    of the active composite mode -- backs the "judged-only" variance
    decomposition, which is always about judge health specifically (spec
    §6), independent of whether judged fields are excluded from the report's
    *main* composite this time."""

    values = [field_scores[f] for f in JUDGED_FIELDS if field_scores[f] is not None]
    if not values:
        return None
    return sum(values) / len(values) * 100.0


def _item_replicate_matrix(
    rows: Sequence[RunRow],
    item_ids: Sequence[str],
    value_fn: Callable[[RunRow], float | None],
) -> list[list[float]] | None:
    """Builds a rectangular item x replicate matrix of ``value_fn(row)`` for
    ``item_ids``, each item's rows ordered by replicate index. Returns
    ``None`` -- a deliberate refusal to guess, rather than pad or drop --
    when the rows aren't rectangular (an item with no rows, a different
    replicate count than the first item, or a row whose ``value_fn`` comes
    back ``None``): ``variance_components`` requires a complete array, and
    silently patching a hole would misrepresent the decomposition."""

    matrix: list[list[float]] = []
    replicate_count: int | None = None
    for item_id in item_ids:
        item_rows = sorted((r for r in rows if r.item_id == item_id), key=lambda r: r.replicate)
        if not item_rows:
            return None
        values: list[float] = []
        for row in item_rows:
            value = value_fn(row)
            if value is None:
                return None
            values.append(value)
        if replicate_count is None:
            replicate_count = len(values)
        elif len(values) != replicate_count:
            return None
        matrix.append(values)
    if not matrix or not replicate_count:
        return None
    return matrix


def _field_accuracy(rows: Sequence[RunRow], field: str) -> tuple[float | None, int, int]:
    """``(accuracy, n_scored, n_missing)`` for one field across ``rows``.
    ``n_missing`` counts judge-error rows (score ``None``); accuracy is the
    mean over the ``n_scored`` rows with a determinate 0/1 score, or
    ``None`` if every row's score for this field is missing."""

    values = [row.field_scores[field] for row in rows]
    scored = [v for v in values if v is not None]
    missing = len(values) - len(scored)
    if not scored:
        return None, 0, missing
    return sum(scored) / len(scored), len(scored), missing


def _score_length_pairs(rows: Sequence[RunRow]) -> tuple[list[float], list[float]]:
    """Pooled ``(score, character length of the candidate's field value)``
    pairs over both judged fields, restricted to rows where that field was
    actually judged and returned a verdict (``field_scores[field] is not
    None``) -- a candidate schema-invalid/refusal row has no judge call and
    no field value to measure, and a judge-error row has no score, so both
    are naturally excluded by this single condition (a candidate failure's
    ``raw_output`` is never valid JSON, and a judge error's score is
    ``None``)."""

    scores: list[float] = []
    lengths: list[float] = []
    for row in rows:
        try:
            output = json.loads(row.raw_output)
        except json.JSONDecodeError:
            continue
        if not isinstance(output, dict):
            continue
        for field in JUDGED_FIELDS:
            score = row.field_scores.get(field)
            if score is None:
                continue
            value = output.get(field)
            scores.append(float(score))
            lengths.append(float(len(str(value))) if value is not None else 0.0)
    return scores, lengths


def _mean_item_composite(
    artifact: RunArtifact, item_ids: Sequence[str], mode: CompositeMode
) -> dict[str, float | None]:
    """Per-item composite, averaged across that item's replicates (rows
    with an undefined row composite -- see ``_row_composite`` -- are
    excluded from the average, not zeroed). ``None`` iff every replicate of
    that item has an undefined composite."""

    result: dict[str, float | None] = {}
    for item_id in item_ids:
        values = [
            v
            for v in (
                _row_composite(row.field_scores, mode) for row in artifact.rows_for_item(item_id)
            )
            if v is not None
        ]
        result[item_id] = sum(values) / len(values) if values else None
    return result


def _majority_vote(scores: Sequence[int | None]) -> str | None:
    """Majority vote ("pass"/"fail") across one (item, field)'s replicate
    0/1 scores. A tie (mean exactly 0.5, only possible at even replicate
    counts) resolves to "pass" (``>= 0.5``). ``None`` iff every replicate's
    score is missing (a judge error on every replicate)."""

    determinate = [s for s in scores if s is not None]
    if not determinate:
        return None
    return "pass" if sum(determinate) / len(determinate) >= 0.5 else "fail"


def _field_flip_stats(
    artifact_a: RunArtifact,
    artifact_b: RunArtifact,
    item_ids: Sequence[str],
    field: str,
) -> tuple[float, float, int, int] | None:
    """``(pass_rate_a, pass_rate_b, fail_to_pass_flips, pass_to_fail_flips)``
    for one field over the items with a determinate majority vote in *both*
    artifacts. ``None`` if no such paired item exists."""

    votes_a: dict[str, str] = {}
    votes_b: dict[str, str] = {}
    for item_id in item_ids:
        vote_a = _majority_vote(
            [row.field_scores[field] for row in artifact_a.rows_for_item(item_id)]
        )
        vote_b = _majority_vote(
            [row.field_scores[field] for row in artifact_b.rows_for_item(item_id)]
        )
        if vote_a is not None:
            votes_a[item_id] = vote_a
        if vote_b is not None:
            votes_b[item_id] = vote_b

    paired = [i for i in item_ids if i in votes_a and i in votes_b]
    if not paired:
        return None

    rate_a = sum(1 for i in paired if votes_a[i] == "pass") / len(paired)
    rate_b = sum(1 for i in paired if votes_b[i] == "pass") / len(paired)
    fail_to_pass = sum(1 for i in paired if votes_a[i] == "fail" and votes_b[i] == "pass")
    pass_to_fail = sum(1 for i in paired if votes_a[i] == "pass" and votes_b[i] == "fail")
    return rate_a, rate_b, fail_to_pass, pass_to_fail


def _untraced_banner(who: str | None = None) -> str:
    suffix = f" ({who})" if who else ""
    return (
        f"> **UNTRACED**{suffix} -- this run has no complete Langfuse trace (spec "
        "§8): it either ran keyless or tracing degraded mid-run. It can never feed "
        "a baseline or the README."
    )


def _certificate_section(
    certificate: Certificate | None, *, reportable: bool
) -> tuple[str, CompositeMode]:
    """The certificate header block every report embeds (spec §5), and the
    composite mode it implies (see module docstring). Returned together so
    callers can never compute the mode from anything other than the exact
    certificate that produced the header text above it.

    Raises ``MissingCertificateError`` when ``reportable`` and ``certificate
    is None``. Otherwise ``certificate is None`` renders the dev-stage
    "uncalibrated" banner.
    """

    if certificate is None:
        if reportable:
            raise MissingCertificateError(
                "This report is reportable (spec §8) and requires a committed "
                "calibration certificate (data/calibration/certificate.json); none "
                "was supplied. A reportable run with no certificate is refused, "
                "not rendered with the uncalibrated banner."
            )
        banner = (
            "> **UNCALIBRATED (no certificate)** -- this is a dev-stage report with "
            "no judge calibration certificate on file. Composite scores below use "
            "FULL_7 (all seven fields) uncritically; judge quality has not been "
            "measured against owner labels. This report can never feed a baseline "
            "or the README (spec §5/§8)."
        )
        return banner, CompositeMode.FULL_7

    mode = _resolve_composite_mode(certificate)
    lines = [
        "## Judge Calibration Certificate",
        "",
        f"- Judge version: `{certificate.judge_version}`",
        f"- Overall κ = {certificate.overall_kappa:.3f} (95% CI "
        f"[{certificate.kappa_ci[0]:.3f}, {certificate.kappa_ci[1]:.3f}])",
        f"- Verdict: **{certificate.verdict}**",
    ]
    if certificate.ceiling_kappa is not None:
        lines.append(
            f"- Test-retest intra-annotator consistency ceiling κ = "
            f"{certificate.ceiling_kappa:.3f}"
        )
    lines.append(
        "- Per-candidate κ (point estimate only -- the certificate carries a CI "
        "for the overall κ above, not per-candidate):"
    )
    for label in sorted(certificate.per_candidate_kappa):
        lines.append(f"  - candidate {label}: κ = {certificate.per_candidate_kappa[label]:.3f}")

    if certificate.verdict == "inadequate":
        lines.append("")
        lines.append(
            "> **Judged fields excluded (DETERMINISTIC_5).** Certificate verdict is "
            "`inadequate`: every composite figure below excludes issue_summary/"
            "requested_action and uses the 5 deterministic fields only (spec §5 "
            "degraded mode)."
        )
    elif certificate.verdict == "adequate_with_caveat":
        lines.append("")
        lines.append(
            "> **Caveat:** κ̂ >= 0.6 but the CI lower bound < 0.4 "
            "(`adequate_with_caveat`) -- flagged per spec §5."
        )

    return "\n".join(lines), mode


# --------------------------------------------------------------------------
# render_run_report
# --------------------------------------------------------------------------


def render_run_report(
    artifact: RunArtifact,
    *,
    certificate: Certificate | None = None,
    reportable: bool = False,
    seed: int = 0,
    n_resamples: int = 10_000,
) -> str:
    """Renders one candidate's run report (spec §6): composite mean per
    slice (nominal/adversarial/all) with 95% BCa cluster CIs, per-field
    accuracies, per-category table, variance decomposition (full +
    judged-only), and score-vs-length correlation. Pure over ``artifact``
    and ``certificate`` -- no API calls, no filesystem access.
    """

    cert_section, mode = _certificate_section(certificate, reportable=reportable)
    items_by_id = {item.id: item for item in artifact.items}
    rows = artifact.rows

    lines: list[str] = [f"# Run Report -- Candidate {artifact.model_key}", "", cert_section, ""]

    if artifact.untraced:
        lines.append(_untraced_banner())
        lines.append("")

    lines.append(f"Composite mode used for every aggregate below: **{mode}**.")
    lines.append("")

    lines.append("## Composite Score by Slice")
    lines.append("")
    lines.append(f"| Slice | Rows | Mean composite | {_RUN_REPORT_CI_LEVEL:.0%} BCa CI |")
    lines.append("|---|---|---|---|")
    slices: list[tuple[str, list[RunRow]]] = [
        ("nominal", [r for r in rows if items_by_id[r.item_id].meta.slice == "nominal"]),
        ("adversarial", [r for r in rows if items_by_id[r.item_id].meta.slice == "adversarial"]),
        ("all", list(rows)),
    ]
    for slice_name, slice_rows in slices:
        composites: list[float] = []
        clusters: list[str] = []
        for row in slice_rows:
            value = _row_composite(row.field_scores, mode)
            if value is None:
                continue
            composites.append(value)
            clusters.append(row.item_id)
        if len(composites) < 2 or len(set(clusters)) < 2:
            lines.append(f"| {slice_name} | {len(slice_rows)} | n/a | insufficient data |")
            continue
        mean_val = sum(composites) / len(composites)
        lo, hi = bca_ci(
            composites,
            level=_RUN_REPORT_CI_LEVEL,
            clusters=clusters,
            seed=seed,
            n_resamples=n_resamples,
        )
        lines.append(
            f"| {slice_name} | {len(slice_rows)} | {mean_val:.2f} | [{lo:.2f}, {hi:.2f}] |"
        )
    lines.append("")

    lines.append("## Per-Field Accuracy")
    lines.append("")
    lines.append("| Field | Scored | Missing (judge error) | Accuracy |")
    lines.append("|---|---|---|---|")
    for field in ALL_FIELDS:
        accuracy, n_scored, n_missing = _field_accuracy(rows, field)
        accuracy_text = f"{accuracy * 100:.1f}%" if accuracy is not None else "n/a"
        lines.append(f"| {field} | {n_scored} | {n_missing} | {accuracy_text} |")
    lines.append("")

    lines.append("## Per-Category")
    lines.append("")
    category_values: dict[str, list[float]] = {}
    for row in rows:
        item = items_by_id[row.item_id]
        value = _row_composite(row.field_scores, mode)
        if value is None:
            continue
        for category in item.meta.categories:
            category_values.setdefault(category, []).append(value)
    lines.append("| Category | Rows | Mean composite |")
    lines.append("|---|---|---|")
    for category in sorted(category_values):
        values = category_values[category]
        lines.append(f"| {category} | {len(values)} | {sum(values) / len(values):.2f} |")
    lines.append("")

    lines.append("## Variance Decomposition")
    lines.append("")
    item_ids = [item.id for item in artifact.items]
    full_matrix = _item_replicate_matrix(
        rows, item_ids, lambda r: _row_composite(r.field_scores, mode)
    )
    judged_matrix = _item_replicate_matrix(
        rows, item_ids, lambda r: _judged_only_composite(r.field_scores)
    )

    lines.append(f"### Full composite ({mode})")
    lines.append("")
    if full_matrix is None:
        lines.append(
            "_Insufficient rectangular item x replicate data (differing replicate "
            "counts, or a row with an undefined composite, per item)._"
        )
    else:
        full_components = variance_components(full_matrix)
        lines.append(f"- Between-item variance: {full_components['between_item']:.3f}")
        lines.append(f"- Between-replicate variance: {full_components['between_replicate']:.3f}")
    lines.append("")

    lines.append("### Judged-fields-only composite")
    lines.append("")
    if judged_matrix is None:
        lines.append(
            "_Insufficient rectangular item x replicate data for the judged-fields-"
            "only decomposition._"
        )
    else:
        judged_components = variance_components(judged_matrix)
        lines.append(f"- Between-item variance: {judged_components['between_item']:.3f}")
        lines.append(
            f"- Between-replicate variance: {judged_components['between_replicate']:.3f}"
        )
    lines.append("")
    lines.append(
        "_Both decompositions use numpy's population convention (`ddof=0`): these "
        "are literal descriptive decompositions of the observed array, not "
        "unbiased estimators of latent random-effects parameters._"
    )
    lines.append("")

    lines.append("## Score-vs-Length Correlation")
    lines.append("")
    scores, lengths = _score_length_pairs(rows)
    r = _pearson_r(scores, lengths)
    r_text = f"{r:.3f}" if r is not None else "n/a (insufficient variance)"
    lines.append(
        "Pearson r between judge verdict (0/1) and candidate field-value character "
        f"length, pooled over the judged fields ({JUDGED_FIELDS[0]}, "
        f"{JUDGED_FIELDS[1]}): **{r_text}** (n={len(scores)} judged-field "
        "observations)."
    )
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------
# render_compare_report
# --------------------------------------------------------------------------


def render_compare_report(
    artifact_a: RunArtifact,
    artifact_b: RunArtifact,
    *,
    certificate: Certificate | None = None,
    reportable: bool = False,
    seed: int = 0,
    n_resamples: int = 10_000,
) -> str:
    """Renders the paired comparison report (spec §6): mean delta with 95%
    BCa CI, two-sided sign-flip permutation p, per-field pass-rate delta
    tables with flip counts (majority vote across replicates), and absolute
    scores alongside deltas. Delta convention: ``candidate_b - candidate_a``
    (module docstring). Pure over both artifacts and ``certificate``.
    """

    cert_section, mode = _certificate_section(certificate, reportable=reportable)
    label_a, label_b = artifact_a.model_key, artifact_b.model_key

    ids_b = {item.id for item in artifact_b.items}
    shared_ids = [item.id for item in artifact_a.items if item.id in ids_b]

    lines: list[str] = [
        f"# Compare Report -- Candidate {label_a} vs Candidate {label_b}",
        "",
        cert_section,
        "",
    ]

    if artifact_a.untraced:
        lines.append(_untraced_banner(f"candidate {label_a}"))
    if artifact_b.untraced:
        lines.append(_untraced_banner(f"candidate {label_b}"))
    if artifact_a.untraced or artifact_b.untraced:
        lines.append("")

    lines.append(f"Composite mode used for every aggregate below: **{mode}**.")
    lines.append("")
    lines.append(f"Shared item set: {len(shared_ids)} item(s).")
    lines.append("")

    composite_a = _mean_item_composite(artifact_a, shared_ids, mode)
    composite_b = _mean_item_composite(artifact_b, shared_ids, mode)
    paired_ids = [
        item_id
        for item_id in shared_ids
        if composite_a[item_id] is not None and composite_b[item_id] is not None
    ]
    deltas = [composite_b[item_id] - composite_a[item_id] for item_id in paired_ids]  # type: ignore[operator]

    lines.append("## Composite Score (absolute, alongside deltas below)")
    lines.append("")
    lines.append(f"| Candidate | Mean composite (n={len(paired_ids)} items) |")
    lines.append("|---|---|")
    if paired_ids:
        mean_a = sum(composite_a[i] for i in paired_ids) / len(paired_ids)  # type: ignore[misc]
        mean_b = sum(composite_b[i] for i in paired_ids) / len(paired_ids)  # type: ignore[misc]
        lines.append(f"| {label_a} | {mean_a:.2f} |")
        lines.append(f"| {label_b} | {mean_b:.2f} |")
    else:
        lines.append(f"| {label_a} | n/a |")
        lines.append(f"| {label_b} | n/a |")
    lines.append("")

    lines.append("## Mean Delta")
    lines.append("")
    if len(deltas) >= 2:
        mean_delta = sum(deltas) / len(deltas)
        lo, hi = bca_ci(deltas, level=_COMPARE_REPORT_CI_LEVEL, seed=seed, n_resamples=n_resamples)
        perm = sign_flip_test(deltas, sided="two", seed=seed, n_resamples=n_resamples)
        lines.append(
            f"Mean delta ({label_b} - {label_a}): **{mean_delta:.2f}** points "
            f"({_COMPARE_REPORT_CI_LEVEL:.0%} BCa CI [{lo:.2f}, {hi:.2f}])."
        )
        lines.append(
            f"Two-sided sign-flip permutation p = {perm.p:.4f} (m = {perm.m_nonzero} "
            f"nonzero deltas, {perm.method}, min attainable p = "
            f"{perm.min_attainable_p:.4f})."
        )
    else:
        lines.append("_Insufficient paired items for a delta/CI/permutation test._")
    lines.append("")

    lines.append("## Per-Field Pass-Rate Delta (majority vote across replicates)")
    lines.append("")
    lines.append(
        f"| Field | {label_a} pass rate | {label_b} pass rate | Delta (pp) | "
        f"fail→pass flips | pass→fail flips |"
    )
    lines.append("|---|---|---|---|---|---|")
    for field in ALL_FIELDS:
        stats = _field_flip_stats(artifact_a, artifact_b, shared_ids, field)
        if stats is None:
            lines.append(f"| {field} | n/a | n/a | n/a | n/a | n/a |")
            continue
        rate_a, rate_b, fail_to_pass, pass_to_fail = stats
        delta_pp = (rate_b - rate_a) * 100
        lines.append(
            f"| {field} | {rate_a * 100:.1f}% | {rate_b * 100:.1f}% | {delta_pp:+.1f} | "
            f"{fail_to_pass} | {pass_to_fail} |"
        )
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------
# render_gate_summary
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CandidateGateResult:
    """One candidate's row in the gate's decision table (spec §7). The gate
    module (T16) computes every field here from its own stats calls
    (``sign_flip_test``, ``bca_ci``, ``mde``) and the baseline comparison;
    this dataclass only carries the already-decided numbers for rendering
    -- ``render_gate_summary`` never recomputes a statistic.

    ``label``: ``"a"`` | ``"b"`` -- which candidate this result describes.
    ``verdict``: this candidate's own decision-rule outcome (``"fail"`` iff
        ``p_value < alpha`` AND the regression magnitude (``-delta``)
        exceeds ``margin`` -- decided by the gate module, not this renderer).
    ``delta``: mean nominal-slice, K-averaged, paired per-item composite
        delta, ``current - baseline`` (module docstring's sign convention;
        negative is a regression).
    ``delta_ci``: two-sided 90% BCa CI on ``delta`` (spec §7: 90% two-sided
        <-> the one-sided 5% test level the decision rule actually uses).
    ``p_value``: one-sided sign-flip permutation p (regression direction),
        from ``sign_flip_test(..., sided="one")``.
    ``m_nonzero``: count of nonzero per-item deltas feeding the permutation
        test (spec §7 sparse-delta disclosure).
    ``min_attainable_p``: floor p attainable at this ``m_nonzero``
        (``PermutationResult.min_attainable_p``) -- backs the m<=4/m=5
        warnings.
    ``permutation_method``: ``"exact"`` | ``"monte_carlo"``.
    ``mde``: minimum detectable effect at alpha=0.05/power=0.80, points
        (``harness.stats.mde.mde``, one-sided convention).
    ``judge_error_excluded``: count of nominal-slice items excluded from
        this candidate's paired deltas because a judged field came back
        missing on at least one replicate (spec §7: missing != fail).
    ``adversarial_delta``: adversarial-slice composite delta, same sign
        convention as ``delta`` -- always printed, whether or not the
        guardrail trips.
    ``adversarial_guardrail_tripped``: True iff the adversarial delta hit
        the coarse >=10-point-drop threshold (spec §7).
    ``usage_candidate``: this candidate's summed candidate-call token usage
        (``RunArtifact.usage_totals()`` shape: ``{"input_tokens",
        "output_tokens"}``).
    ``usage_judge``: summed judge-call token usage incurred scoring this
        candidate's run (``RunArtifact.judge_usage_totals()``).
    ``untraced``: this candidate's run artifact's ``untraced`` flag --
        drives the untraced banner for this candidate.
    """

    label: Literal["a", "b"]
    verdict: Literal["pass", "fail"]
    delta: float
    delta_ci: tuple[float, float]
    p_value: float
    m_nonzero: int
    min_attainable_p: float
    permutation_method: Literal["exact", "monte_carlo"]
    mde: float
    judge_error_excluded: int
    adversarial_delta: float
    adversarial_guardrail_tripped: bool
    usage_candidate: dict[str, int]
    usage_judge: dict[str, int]
    untraced: bool


@dataclass(frozen=True, slots=True)
class GateSummaryData:
    """Everything ``render_gate_summary`` needs, already computed by the
    gate module (T16) -- a small, explicit dataclass rather than raw
    ``RunArtifact``/baseline objects, so this renderer stays a pure
    formatting function with zero knowledge of how the gate decision was
    reached, and so T16 has one clear contract to construct against.

    ``certificate``: the committed calibration certificate, or ``None`` for
        an "uncalibrated" dev-stage render (disallowed when ``reportable``
        is True -- gate runs are always reportable in practice, spec §8,
        but the flag stays explicit here for testability and symmetry with
        the other two renderers).
    ``reportable``: see ``certificate``.
    ``composite_mode``: the composite definition actually used to compute
        every delta/MDE/adversarial-delta figure carried in ``candidates``
        (``FULL_7`` normally; ``DETERMINISTIC_5`` when
        ``certificate.verdict == "inadequate"``) -- trusted verbatim rather
        than re-derived, since the gate module is the one that actually
        ran the numbers under this mode (module docstring).
    ``margin``: ``config.gate.margin`` (points) -- printed verbatim (D3).
    ``alpha``: ``config.gate.alpha`` -- printed verbatim (D3).
    ``k``: ``config.k`` (this gate run's per-item replicate count, not
        ``k_baseline``) -- printed verbatim (D3).
    ``price_snapshot``: ``config.price_snapshot`` -- dated list prices used
        to approximate candidate and judge cost separately.
    ``candidates``: one ``CandidateGateResult`` per candidate, in ``"a"``,
        ``"b"`` order.
    ``overall_verdict``: ``"fail"`` iff ANY candidate's own verdict is
        ``"fail"`` (spec §7: "the gate fails if the rule fires for either
        candidate") -- decided by the gate module, carried verbatim here.
    """

    certificate: Certificate | None
    reportable: bool
    composite_mode: CompositeMode
    margin: float
    alpha: float
    k: int
    price_snapshot: PriceSnapshot
    candidates: tuple[CandidateGateResult, ...]
    overall_verdict: Literal["pass", "fail"]


def _candidate_price(price_snapshot: PriceSnapshot, label: str) -> ModelPrice:
    return price_snapshot.candidate_a if label == "a" else price_snapshot.candidate_b


def _cost_usd(usage: Mapping[str, int], price: ModelPrice) -> float:
    return (
        usage.get("input_tokens", 0) / 1_000_000 * price.input_per_mtok
        + usage.get("output_tokens", 0) / 1_000_000 * price.output_per_mtok
    )


def _sparse_delta_warning(m_nonzero: int) -> str | None:
    """Spec §7 sparse-delta disclosure, whenever ``m < 6``: at ``m <= 4`` no
    rejection is possible at alpha=0.05 regardless of regression size; at
    ``m == 5`` rejection requires all five nonzero deltas to be regressions
    (min p = 0.031, errata 2026-07-04). ``None`` when ``m_nonzero >= 6``."""

    if m_nonzero <= 4:
        return (
            f"**Sparse-delta warning (m={m_nonzero}):** no rejection is possible at "
            "α=0.05 regardless of regression size (m <= 4)."
        )
    if m_nonzero == 5:
        return (
            "**Sparse-delta warning (m=5):** rejection requires all five nonzero "
            "deltas to be regressions (min p = 0.031)."
        )
    return None


def render_gate_summary(data: GateSummaryData) -> str:
    """Renders the CI gate summary (spec §7): verdict per candidate, delta
    + 90% BCa CI, one-sided p, m + sparse-delta warnings, MDE, judge-error
    exclusion count, adversarial delta (always) + guardrail status, the
    family false-alarm line, config values used, token totals + approximate
    cost (candidate and judge priced separately), and the relative link to
    ``docs/gate-design.md``. Pure formatting over ``data`` -- computes no
    statistic itself (module docstring).
    """

    cert_section, _ = _certificate_section(data.certificate, reportable=data.reportable)

    lines: list[str] = ["# Gate Summary", "", cert_section, ""]

    untraced_labels = [c.label for c in data.candidates if c.untraced]
    for label in untraced_labels:
        lines.append(_untraced_banner(f"candidate {label}"))
    if untraced_labels:
        lines.append("")

    lines.append(f"**Overall verdict: {data.overall_verdict.upper()}**")
    lines.append("")
    lines.append(f"Composite mode used for every figure below: **{data.composite_mode}**.")
    lines.append("")

    lines.append("## Config")
    lines.append("")
    lines.append(f"- margin: {data.margin}")
    lines.append(f"- alpha: {data.alpha}")
    lines.append(f"- K: {data.k}")
    lines.append("")

    lines.append("## Per-Candidate Results")
    lines.append("")
    for candidate in data.candidates:
        lines.append(f"### Candidate {candidate.label}")
        lines.append("")
        lines.append(f"- Verdict: **{candidate.verdict.upper()}**")
        lines.append(
            "- Mean delta (nominal slice, current - baseline): "
            f"**{candidate.delta:.2f}** points (90% BCa CI "
            f"[{candidate.delta_ci[0]:.2f}, {candidate.delta_ci[1]:.2f}])¹"
        )
        lines.append(
            f"- One-sided sign-flip permutation p = {candidate.p_value:.4f} "
            f"(m = {candidate.m_nonzero} nonzero deltas, {candidate.permutation_method}, "
            f"min attainable p = {candidate.min_attainable_p:.4f})"
        )
        warning = _sparse_delta_warning(candidate.m_nonzero)
        if warning is not None:
            lines.append(f"- {warning}")
        lines.append(f"- MDE (α={data.alpha}, 80% power): **{candidate.mde:.2f}** points")
        lines.append(
            f"- Judge-error exclusions: {candidate.judge_error_excluded} item(s) excluded "
            "from paired deltas (a missing judged field is never scored as a fail)"
        )
        guardrail_text = "TRIPPED" if candidate.adversarial_guardrail_tripped else "not tripped"
        lines.append(
            "- Adversarial-slice delta (current - baseline, always printed): "
            f"**{candidate.adversarial_delta:.2f}** points -- coarse guardrail "
            f"(>=10-point drop): **{guardrail_text}**"
        )
        candidate_price = _candidate_price(data.price_snapshot, candidate.label)
        cost_candidate = _cost_usd(candidate.usage_candidate, candidate_price)
        cost_judge = _cost_usd(candidate.usage_judge, data.price_snapshot.judge)
        lines.append(
            f"- Candidate token usage: {candidate.usage_candidate.get('input_tokens', 0)} in / "
            f"{candidate.usage_candidate.get('output_tokens', 0)} out "
            f"(~${cost_candidate:.4f}, {data.price_snapshot.label} "
            f"{data.price_snapshot.date.isoformat()})"
        )
        lines.append(
            f"- Judge token usage: {candidate.usage_judge.get('input_tokens', 0)} in / "
            f"{candidate.usage_judge.get('output_tokens', 0)} out (~${cost_judge:.4f}; "
            "judge calls dominate cost -- one call per judged field per replicate, "
            "vs one candidate call per replicate)"
        )
        lines.append("")

    lines.append("## Family False-Alarm Rate")
    lines.append("")
    lines.append(
        "Family false-alarm rate: two tests at α=0.05 → ≤ ~9.8% worst case "
        "(union bound over both candidates' independent decision rules: "
        "1 - (1 - 0.05)² ≈ 0.0975)."
    )
    lines.append("")

    lines.append("## Further Reading")
    lines.append("")
    lines.append(
        f"See [{GATE_DESIGN_DOC_LINK}]({GATE_DESIGN_DOC_LINK}) for the analytic "
        "false-alarm justification, threat model, and re-baseline procedure."
    )
    lines.append("")

    lines.append("---")
    lines.append(
        "¹ _This summary's delta CIs are 90% two-sided, which corresponds to the "
        "one-sided 5% significance level the decision rule actually tests against -- "
        "`eval run`/`eval compare` reports use 95%._"
    )

    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "CandidateGateResult",
    "GateSummaryData",
    "MissingCertificateError",
    "render_compare_report",
    "render_gate_summary",
    "render_run_report",
]
