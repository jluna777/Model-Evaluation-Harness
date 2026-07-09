"""Standard normal CDF/quantile -- shared by ``bootstrap.py`` (BCa's
bias-correction/acceleration adjustment) and ``mde.py`` (the one-sided
``z_alpha``/``z_beta`` quantiles), without a scipy dependency in ``src/``.

Promoted out of ``bootstrap.py`` (where these were originally private
helpers) so ``mde.py`` no longer reaches across module boundaries into
another module's private (``_``-prefixed) names -- both now import from this
shared home instead. Pure relocation: no behavior change.
"""

import math

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
