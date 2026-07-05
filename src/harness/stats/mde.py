"""Minimum detectable effect for the CI gate's one-sided test (spec §7, D3).

The gate's decision rule is a one-sided sign-flip permutation test (spec §7:
"one-sided sign-flip permutation test on K-averaged paired per-item deltas
vs baseline"). The MDE this module reports must match that one-sided
convention exactly, or the printed "catches >=X-point regressions at 80%
power" claim would silently describe a different (two-sided) test than the
one the gate actually runs.

Formula: ``mde = (z_alpha + z_beta) * delta_sd / sqrt(n)``, the standard
one-sample/paired-difference MDE at significance ``alpha`` and power
``power`` (Cohen 1988). The binding precision point is ``z_alpha``: it is
the **one-sided** ``(1 - alpha)`` quantile of the standard normal (1.6448...
at alpha=0.05), not the two-sided ``(1 - alpha/2)`` quantile (1.9600...) --
using the two-sided quantile here would overstate the effect size the gate
can actually detect, since the gate's own test is one-sided.

``z_alpha``/``z_beta`` reuse :func:`harness.stats.bootstrap._norm_ppf`
(Acklam's rational approximation + Halley refinement) rather than importing
scipy, keeping scipy a dev-only dependency for ``src/``.
"""

from __future__ import annotations

import math

from harness.stats.bootstrap import _norm_ppf


def mde(delta_sd: float, n: int, *, alpha: float = 0.05, power: float = 0.80) -> float:
    """Minimum detectable effect at significance ``alpha`` and ``power``.

    ``delta_sd`` is the observed standard deviation of the per-item paired
    deltas; ``n`` is the number of paired items. ``z_alpha`` is deliberately
    the **one-sided** ``(1 - alpha)`` quantile (matching the gate's one-sided
    sign-flip test, spec §7/D3) -- not the two-sided ``(1 - alpha/2)``
    quantile.
    """

    if n <= 0:
        raise ValueError(f"n must be positive, got {n!r}")
    if delta_sd < 0.0:
        raise ValueError(f"delta_sd must be non-negative, got {delta_sd!r}")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha!r}")
    if not 0.0 < power < 1.0:
        raise ValueError(f"power must be in (0, 1), got {power!r}")

    z_alpha = _norm_ppf(1.0 - alpha)
    z_beta = _norm_ppf(power)
    return (z_alpha + z_beta) * delta_sd / math.sqrt(n)
