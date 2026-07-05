"""Cohen's kappa with cluster-bootstrap CI (judge-human agreement, spec §5, D2).

Cohen's kappa is the single statistic that decides judge-calibration
adequacy (constitution §5 conformance: one agreement method, not several --
Gwet's AC1 was considered and dropped, D2 amendment 2026-07-04a). Raw
agreement and label prevalence are reported alongside it as descriptive
context only; spec §5 is explicit that they never decide anything.

Labels are the harness's binary pass/fail verdict strings (schema.py's
``CalibrationLabel.verdict``). By calling convention ``a`` is the reference
(owner/human) label sequence and ``b`` is the judge's verdicts -- this
mirrors the eventual ``eval calibrate`` call, ``cohens_kappa(owner_labels,
judge_labels)``. ``prevalence`` reports the fraction of the positive class
("pass") within ``a`` specifically (documented here since the interface
takes two symmetric-looking sequences): curated calibration sets skew toward
"pass" (D2), the regime where kappa is most needed because raw agreement
alone is misleading. ``raw_agreement`` is the fraction of paired positions
where the two sequences agree exactly (observed agreement, ``p_o``).

All calibration CIs are cluster-bootstrap resampling emails (D2 amendment
2026-07-04a): judgments within one email are correlated (fields and
candidates within an email share context), so naive per-observation
resampling understates uncertainty. This module builds the CI by delegating
to :func:`harness.stats.bootstrap.bca_ci` over an array of *index positions*
rather than the labels themselves: ``bca_ci`` only knows how to resample and
reduce a plain 1-D array, so the "statistic" passed to it is a closure that
looks up ``a``/``b`` at the resampled positions and recomputes kappa on that
subset. Passing ``clusters`` straight through makes ``bca_ci`` resample whole
clusters of index positions together -- exactly "resample clusters of index
positions, recompute kappa."

Degenerate single-category convention: standard Cohen's kappa is undefined
(0/0) whenever both label sequences collapse onto one shared category --
chance agreement ``p_e`` hits 1, so ``(p_o - p_e) / (1 - p_e)`` divides by
zero. This mirrors ``sklearn.metrics.cohen_kappa_score``'s own default
(``replace_undefined_by=nan``): the chosen convention here is to return
``float("nan")`` for ``kappa`` rather than raising, so a degenerate
calibration slice (or an unlucky bootstrap resample that happens to draw an
all-one-category subsample) flows through reporting code instead of
crashing. Callers that need to react to an undefined kappa check
``math.isnan(result.kappa)``.
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Sequence
from dataclasses import dataclass

import numpy as np

from harness.stats.bootstrap import bca_ci

_POSITIVE_LABEL = "pass"


@dataclass(frozen=True, slots=True)
class KappaResult:
    """Result of :func:`cohens_kappa`.

    ``ci`` is the two-sided cluster-bootstrap confidence interval on kappa
    (spec §5's +/-0.15-0.25 pre-clustering resolution floor may widen once
    clustered). ``raw_agreement`` and ``prevalence`` are descriptive context
    only -- never a decision input (constitution §5 conformance).
    """

    kappa: float
    ci: tuple[float, float]
    raw_agreement: float
    prevalence: float


def cohens_kappa(
    a: Sequence[str],
    b: Sequence[str],
    *,
    clusters: Sequence[Hashable] | None = None,
    level: float = 0.95,
    n_resamples: int = 10_000,
    seed: int = 0,
) -> KappaResult:
    """Cohen's kappa between two paired pass/fail label sequences.

    ``a`` and ``b`` are same-length sequences of ``"pass"``/``"fail"``
    verdicts; by convention ``a`` is the reference/human labels and ``b`` the
    judge's (see module docstring for why this matters for ``prevalence``).
    ``clusters``, when given, is a per-position label (e.g. the email each
    judgment belongs to) used only for the confidence interval's cluster
    bootstrap (spec §5, D2) -- the kappa point estimate itself does not
    depend on clustering.
    """

    a_arr = np.asarray(a)
    b_arr = np.asarray(b)
    if a_arr.shape != b_arr.shape:
        raise ValueError("a and b must be the same length")
    if a_arr.ndim != 1:
        raise ValueError("a and b must be 1-D label sequences")
    n = a_arr.size
    if n < 2:
        raise ValueError("a and b must have at least 2 paired observations")

    labels = sorted(set(a_arr.tolist()) | set(b_arr.tolist()))
    code = {label: i for i, label in enumerate(labels)}
    a_codes = np.array([code[x] for x in a_arr.tolist()], dtype=np.int64)
    b_codes = np.array([code[x] for x in b_arr.tolist()], dtype=np.int64)
    n_labels = len(labels)

    kappa = _kappa_point_estimate(a_codes, b_codes, n_labels)
    raw_agreement = float(np.mean(a_arr == b_arr))
    prevalence = float(np.mean(a_arr == _POSITIVE_LABEL))

    def statistic(idx: np.ndarray) -> float:
        positions = idx.astype(np.int64)
        return _kappa_point_estimate(a_codes[positions], b_codes[positions], n_labels)

    idx_values = np.arange(n, dtype=np.float64)
    ci = bca_ci(
        idx_values,
        statistic,
        level=level,
        clusters=clusters,
        n_resamples=n_resamples,
        seed=seed,
    )

    return KappaResult(kappa=kappa, ci=ci, raw_agreement=raw_agreement, prevalence=prevalence)


def _kappa_point_estimate(a_codes: np.ndarray, b_codes: np.ndarray, n_labels: int) -> float:
    """Unweighted Cohen's kappa from two same-length integer-coded label arrays.

    Matches ``sklearn.metrics.cohen_kappa_score``'s algorithm: build the
    confusion matrix, derive observed agreement ``p_o`` (trace / n) and
    chance agreement ``p_e`` (sum of row-marginal * column-marginal
    fractions), then ``(p_o - p_e) / (1 - p_e)``. Returns ``nan`` when
    ``p_e == 1`` (degenerate: both sequences collapse onto one shared
    category) -- the convention documented at module level.
    """

    n = a_codes.size
    flat = a_codes * n_labels + b_codes
    confusion = np.bincount(flat, minlength=n_labels * n_labels).reshape(n_labels, n_labels)
    confusion = confusion.astype(np.float64)

    row_sums = confusion.sum(axis=1)
    col_sums = confusion.sum(axis=0)
    po = float(np.trace(confusion)) / n
    pe = float(np.sum((row_sums / n) * (col_sums / n)))

    denom = 1.0 - pe
    if math.isclose(denom, 0.0, abs_tol=1e-12):
        return float("nan")
    return (po - pe) / denom
