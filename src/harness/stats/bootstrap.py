"""BCa bootstrap confidence intervals with optional cluster resampling.

Implements the bias-corrected and accelerated (BCa) bootstrap (Efron &
Tibshirani 1993, ch. 14) as a pure statistics function. This module backs
every confidence interval the harness reports: per-model score CIs, gate
delta error bars (spec §7, 90% two-sided), and calibration kappa CIs (spec
§5/§6, 95% cluster CIs).

Cluster mode (``clusters`` not None) exists because judgments within one
email are correlated -- deterministic scoring and judge calls on the same
email's fields/candidates are not independent draws. Resampling then draws
*whole clusters* with replacement: the number of clusters drawn on each
bootstrap iteration equals the number of distinct original clusters, and
every member of a drawn cluster enters the resample together (so a cluster
drawn twice contributes all of its members twice, and a cluster not drawn
contributes nothing). This is the standard cluster bootstrap and reflects
the smaller effective sample size of ``n`` correlated observations sitting
in ``k < n`` clusters. The jackknife used for the acceleration term mirrors
this: leave-one-observation-out normally, leave-one-*cluster*-out here.

BCa precision points:
  - Bias-correction ``z0`` comes from the proportion of bootstrap statistics
    strictly below the observed statistic, passed through the inverse
    standard normal CDF. If that proportion is exactly 0 or 1 -- every
    bootstrap replicate landed on, or on one side of, the observed
    statistic (e.g. constant input data) -- the inverse is +/-infinity and
    BCa is undefined. This implementation detects that and falls back to
    the plain percentile bootstrap CI instead of raising.
  - Acceleration ``a`` comes from the jackknife: leave-one-observation-out
    when ``clusters`` is None, leave-one-cluster-out when it is given.
  - The normal CDF/quantile (``harness.stats._normal``) is implemented
    locally (``math.erf`` for the CDF; Acklam's rational approximation plus
    one Halley refinement step for the quantile/ppf) rather than imported
    from scipy, which is a dev-only dependency for this project.
  - A bootstrap replicate statistic (or a jackknife leave-one-out
    statistic) can itself come back NaN for statistics with a documented
    degenerate-input convention (e.g. :func:`harness.stats.agreement.
    cohens_kappa`'s single-shared-category kappa=nan case) -- an unlucky
    resample can land entirely on a degenerate subset even when the full
    sample is not degenerate. Feeding a NaN straight into ``np.percentile``
    silently returns ``(nan, nan)``. ``nan_policy`` controls what happens
    instead: ``"raise"`` (default) fails loudly, since a silent NaN CI is
    strictly worse than an exception. ``"omit"`` drops the NaN replicates
    (and NaN jackknife values) and proceeds on the rest, disclosing the
    omission via a ``RuntimeWarning`` -- appropriate only for statistics
    with a *known, documented* degenerate-resample convention. Omission
    conditions the CI on non-degenerate resamples, a small bias that is
    accepted here in exchange for disclosure rather than a silent failure.
"""

import math
import warnings
from collections.abc import Callable, Hashable, Sequence
from typing import Literal

import numpy as np

from harness.stats._normal import _norm_cdf, _norm_ppf


def _bca_adjust(z0: float, a: float, z: float) -> float:
    """BCa-adjusted z-value: ``z0 + (z0 + z) / (1 - a * (z0 + z))``."""

    denom = 1.0 - a * (z0 + z)
    if denom == 0.0:
        return math.inf if (z0 + z) > 0.0 else -math.inf
    return z0 + (z0 + z) / denom


def _group_by_cluster(
    values: np.ndarray, clusters: Sequence[Hashable]
) -> list[np.ndarray]:
    """``values`` grouped into one array per distinct cluster label."""

    groups: dict[Hashable, list[float]] = {}
    for value, label in zip(values, clusters, strict=True):
        groups.setdefault(label, []).append(value)
    return [np.asarray(members, dtype=np.float64) for members in groups.values()]


def bca_ci(
    values: Sequence[float],
    statistic: Callable[[np.ndarray], float] = np.mean,
    *,
    level: float,
    clusters: Sequence[Hashable] | None = None,
    n_resamples: int = 10_000,
    seed: int,
    nan_policy: Literal["raise", "omit"] = "raise",
) -> tuple[float, float]:
    """Bias-corrected and accelerated (BCa) bootstrap confidence interval.

    Returns the two-sided ``(lo, hi)`` interval at confidence ``level``
    (e.g. ``0.95``). ``statistic`` is called on a 1-D array of (resampled)
    values and must return a scalar; it defaults to ``np.mean``.

    When ``clusters`` is given (a per-value label sequence the same length
    as ``values``), every bootstrap iteration resamples whole clusters with
    replacement -- drawing as many clusters as there are distinct labels --
    instead of individual values, so correlated within-cluster observations
    always move together. ``clusters=None`` is plain per-observation BCa.

    ``seed`` makes the bootstrap resampling, and therefore the returned
    interval, fully reproducible: the same ``seed`` (with the same other
    arguments) always yields an identical ``(lo, hi)``.

    ``nan_policy`` controls what happens when a bootstrap replicate or
    jackknife leave-one-out statistic comes back NaN (see the module
    docstring's BCa precision points for why that can happen even when
    ``statistic(values)`` itself is finite):

      - ``"raise"`` (default): raises ``ValueError`` naming how many
        replicates (and jackknife values, if any) are NaN, and suggests
        ``nan_policy="omit"`` for statistics with a documented degenerate
        resample convention (e.g. :func:`harness.stats.agreement.
        cohens_kappa`).
      - ``"omit"``: drops the NaN bootstrap replicates before the
        percentile step, and computes the bias-correction proportion
        (``z0``) over the valid (non-NaN) replicates only -- the valid
        count, not the original ``n_resamples``, is the denominator. NaN
        jackknife values are dropped from the acceleration term the same
        way. Exactly one ``RuntimeWarning`` is emitted naming how many of
        how many replicates (and jackknife values) were omitted. This
        conditions the returned CI on non-degenerate resamples, a small
        bias accepted here in exchange for disclosure. If *every*
        bootstrap replicate (or every jackknife value) is NaN even after
        omission, ``ValueError`` is raised regardless of policy -- there
        is nothing left to compute a CI from.

    ``statistic(values)`` itself being NaN always raises ``ValueError``,
    under either policy: that is not a degenerate-resample artifact, it
    means the observed point estimate is undefined.
    """

    values_arr = np.asarray(values, dtype=np.float64)
    if values_arr.size < 2:
        raise ValueError("values must have at least 2 observations")
    if not 0.0 < level < 1.0:
        raise ValueError(f"level must be in (0, 1), got {level!r}")
    if n_resamples < 1:
        raise ValueError("n_resamples must be >= 1")
    if nan_policy not in ("raise", "omit"):
        raise ValueError(f"nan_policy must be 'raise' or 'omit', got {nan_policy!r}")

    rng = np.random.default_rng(seed)
    observed = float(statistic(values_arr))
    if math.isnan(observed):
        raise ValueError("observed statistic is NaN; cannot compute a BCa CI")

    if clusters is None:
        boot_stats, jack_stats = _resample_plain(values_arr, statistic, rng, n_resamples)
    else:
        if len(clusters) != values_arr.size:
            raise ValueError("clusters must be the same length as values")
        groups = _group_by_cluster(values_arr, clusters)
        if len(groups) < 2:
            raise ValueError("clusters must contain at least 2 distinct labels")
        boot_stats, jack_stats = _resample_clustered(groups, statistic, rng, n_resamples)

    boot_stats, jack_stats = _handle_nan_replicates(boot_stats, jack_stats, nan_policy)

    alpha = 1.0 - level
    lo_pct, hi_pct = _bca_percentiles(boot_stats, jack_stats, observed, alpha)
    lo = float(np.percentile(boot_stats, 100.0 * lo_pct))
    hi = float(np.percentile(boot_stats, 100.0 * hi_pct))
    return lo, hi


def _handle_nan_replicates(
    boot_stats: np.ndarray,
    jack_stats: np.ndarray,
    nan_policy: Literal["raise", "omit"],
) -> tuple[np.ndarray, np.ndarray]:
    """Apply ``nan_policy`` to NaN bootstrap/jackknife statistics.

    Returns ``(boot_stats, jack_stats)`` unchanged when neither contains a
    NaN. Otherwise either raises (policy ``"raise"``, or any policy if
    omission would leave nothing to compute from) or returns the NaN-free
    arrays after emitting exactly one ``RuntimeWarning`` (policy ``"omit"``).
    """

    boot_nan_mask = np.isnan(boot_stats)
    jack_nan_mask = np.isnan(jack_stats)
    n_nan_boot = int(boot_nan_mask.sum())
    n_nan_jack = int(jack_nan_mask.sum())

    if n_nan_boot == 0 and n_nan_jack == 0:
        return boot_stats, jack_stats

    if nan_policy == "raise":
        # Name only the nonzero counts (mirrors the "omit" disclosure below):
        # a jackknife-only-NaN case (n_nan_boot == 0) must not be reported as
        # "0 of N bootstrap replicates are NaN" -- that falsely implies the
        # bootstrap replicates are the (or a) problem when they are not.
        parts = []
        if n_nan_boot:
            parts.append(
                f"{n_nan_boot} of {boot_stats.size} bootstrap replicate statistics are NaN"
            )
        if n_nan_jack:
            parts.append(f"{n_nan_jack} of {jack_stats.size} jackknife statistics are NaN")
        raise ValueError(
            "; ".join(parts)
            + " (degenerate resample). Pass nan_policy='omit' for statistics with a known "
            "degenerate resample convention."
        )

    valid_boot = boot_stats[~boot_nan_mask]
    valid_jack = jack_stats[~jack_nan_mask]
    if valid_boot.size == 0:
        raise ValueError(
            f"all {boot_stats.size} bootstrap replicate statistics are NaN; cannot compute a "
            "BCa CI even with nan_policy='omit'"
        )
    if valid_jack.size == 0:
        raise ValueError(
            f"all {jack_stats.size} jackknife statistics are NaN; cannot compute a BCa CI even "
            "with nan_policy='omit'"
        )

    omitted_parts = []
    if n_nan_boot:
        omitted_parts.append(f"{n_nan_boot} of {boot_stats.size} bootstrap replicates")
    if n_nan_jack:
        omitted_parts.append(f"{n_nan_jack} of {jack_stats.size} jackknife values")
    warnings.warn(
        "Omitted " + " and ".join(omitted_parts) + " with NaN statistics (degenerate resample); "
        "the BCa CI now conditions on non-degenerate resamples.",
        RuntimeWarning,
        stacklevel=3,
    )
    return valid_boot, valid_jack


def _resample_plain(
    values: np.ndarray,
    statistic: Callable[[np.ndarray], float],
    rng: np.random.Generator,
    n_resamples: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Per-observation bootstrap draws + leave-one-observation-out jackknife."""

    n = values.size
    boot_stats = np.empty(n_resamples, dtype=np.float64)
    for b in range(n_resamples):
        draw = rng.integers(0, n, size=n)
        boot_stats[b] = statistic(values[draw])

    jack_stats = np.empty(n, dtype=np.float64)
    keep = np.ones(n, dtype=bool)
    for i in range(n):
        keep[i] = False
        jack_stats[i] = statistic(values[keep])
        keep[i] = True

    return boot_stats, jack_stats


def _resample_clustered(
    groups: list[np.ndarray],
    statistic: Callable[[np.ndarray], float],
    rng: np.random.Generator,
    n_resamples: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Whole-cluster bootstrap draws + leave-one-cluster-out jackknife."""

    k = len(groups)
    boot_stats = np.empty(n_resamples, dtype=np.float64)
    for b in range(n_resamples):
        draw = rng.integers(0, k, size=k)
        resample = np.concatenate([groups[i] for i in draw])
        boot_stats[b] = statistic(resample)

    jack_stats = np.empty(k, dtype=np.float64)
    for i in range(k):
        remaining = np.concatenate([groups[j] for j in range(k) if j != i])
        jack_stats[i] = statistic(remaining)

    return boot_stats, jack_stats


def _bca_percentiles(
    boot_stats: np.ndarray, jack_stats: np.ndarray, observed: float, alpha: float
) -> tuple[float, float]:
    """The two BCa-adjusted percentiles (in ``[0, 1]``) to read off ``boot_stats``.

    Falls back to the plain percentile bootstrap (``alpha/2``, ``1 -
    alpha/2``) when the bias-correction proportion is exactly 0 or 1: the
    degenerate case where every bootstrap replicate lands on (or on one
    side of) the observed statistic, e.g. constant input data. There, z0
    would be +/-infinity and the BCa adjustment is undefined.

    Tie handling: ``p0`` uses Efron's strict-``<`` convention (replicates
    exactly equal to the observed statistic do not count as "below"). On
    heavily tied bootstrap distributions -- kappa over pass/fail labels
    produces many -- this is a deliberate convention choice, not an
    accident; alternatives (counting half the ties) shift z0 slightly but
    stay within the interval's own Monte-Carlo noise.
    """

    p0 = float(np.mean(boot_stats < observed))
    if p0 <= 0.0 or p0 >= 1.0:
        return alpha / 2.0, 1.0 - alpha / 2.0

    z0 = _norm_ppf(p0)

    theta_dot = float(jack_stats.mean())
    diffs = theta_dot - jack_stats
    numerator = float(np.sum(diffs**3))
    denominator = 6.0 * float(np.sum(diffs**2)) ** 1.5
    a = numerator / denominator if denominator != 0.0 else 0.0

    z_lo = _norm_ppf(alpha / 2.0)
    z_hi = _norm_ppf(1.0 - alpha / 2.0)
    lo_pct = _norm_cdf(_bca_adjust(z0, a, z_lo))
    hi_pct = _norm_cdf(_bca_adjust(z0, a, z_hi))
    return lo_pct, hi_pct
