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
  - The normal CDF/quantile is implemented locally (``math.erf`` for the
    CDF; Acklam's rational approximation plus one Halley refinement step
    for the quantile/ppf) rather than imported from scipy, which is a
    dev-only dependency for this project.
"""

import math
from collections.abc import Callable, Hashable, Sequence

import numpy as np

# Acklam's rational approximation coefficients for the standard normal
# quantile function (ppf). See P.J. Acklam, "An algorithm for computing the
# inverse normal cumulative distribution function".
_ACKLAM_A = (
    -3.969683028665376e01,
    2.209460984245205e02,
    -2.759285104469687e02,
    1.383577518672690e02,
    -3.066479806614716e01,
    2.506628277459239e00,
)
_ACKLAM_B = (
    -5.447609879822406e01,
    1.615858368580409e02,
    -1.556989798598866e02,
    6.680131188771972e01,
    -1.328068155288572e01,
)
_ACKLAM_C = (
    -7.784894002430293e-03,
    -3.223964580411365e-01,
    -2.400758277161838e00,
    -2.549732539343734e00,
    4.374664141464968e00,
    2.938163982698783e00,
)
_ACKLAM_D = (
    7.784695709041462e-03,
    3.224671290700398e-01,
    2.445134137142996e00,
    3.754408661907416e00,
)
_ACKLAM_P_LOW = 0.02425


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via ``math.erf`` (no scipy dependency)."""

    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (quantile function).

    Acklam's rational approximation, refined with one Halley step against
    the ``math.erf``-based CDF for full double precision. Returns +/-inf at
    the p=0/p=1 boundary rather than raising, matching how
    ``scipy.stats.norm.ppf`` treats those inputs.
    """

    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf

    p_low = _ACKLAM_P_LOW
    p_high = 1.0 - p_low
    a, b, c, d = _ACKLAM_A, _ACKLAM_B, _ACKLAM_C, _ACKLAM_D

    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        x = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    elif p <= p_high:
        q = p - 0.5
        r = q * q
        x = (
            ((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]
        ) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )

    # Halley refinement (per Acklam's note): pushes the ~1.15e-9 relative
    # error of the rational approximation down to full double precision.
    e = _norm_cdf(x) - p
    u = e * math.sqrt(2.0 * math.pi) * math.exp(x * x / 2.0)
    return x - u / (1.0 + x * u / 2.0)


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
    """

    values_arr = np.asarray(values, dtype=np.float64)
    if values_arr.size < 2:
        raise ValueError("values must have at least 2 observations")
    if not 0.0 < level < 1.0:
        raise ValueError(f"level must be in (0, 1), got {level!r}")
    if n_resamples < 1:
        raise ValueError("n_resamples must be >= 1")

    rng = np.random.default_rng(seed)
    observed = float(statistic(values_arr))

    if clusters is None:
        boot_stats, jack_stats = _resample_plain(values_arr, statistic, rng, n_resamples)
    else:
        if len(clusters) != values_arr.size:
            raise ValueError("clusters must be the same length as values")
        groups = _group_by_cluster(values_arr, clusters)
        if len(groups) < 2:
            raise ValueError("clusters must contain at least 2 distinct labels")
        boot_stats, jack_stats = _resample_clustered(groups, statistic, rng, n_resamples)

    alpha = 1.0 - level
    lo_pct, hi_pct = _bca_percentiles(boot_stats, jack_stats, observed, alpha)
    lo = float(np.percentile(boot_stats, 100.0 * lo_pct))
    hi = float(np.percentile(boot_stats, 100.0 * hi_pct))
    return lo, hi


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
