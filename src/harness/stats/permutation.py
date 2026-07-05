"""Sign-flip permutation test (spec §7).

Tests whether a set of paired per-item deltas (``current - baseline``) is
extreme under the null hypothesis that each delta's sign is equally likely
to be positive or negative -- i.e. that there is no true, sign-consistent
effect. The one-sided variant tests the *regression* direction (deltas more
negative than chance would produce) and backs the CI gate; the two-sided
variant tests either direction and serves ``eval compare``.

Zero-valued deltas carry no sign to flip -- multiplying a zero by +/-1 is a
no-op -- so they are excluded from the ``2**m`` enumeration/resampling space.
They are not dropped from the statistic itself, though: the test statistic
is the mean over *all* deltas (zeros included, diluting the mean toward
zero), while only the ``m_nonzero`` nonzero deltas have their sign flipped
to build the null distribution.

Exact mode enumerates all ``2**m_nonzero`` sign assignments when
``m_nonzero <= 20`` (a hair over one million, cheap with vectorized numpy).
Above that, a seeded Monte Carlo draw of ``n_resamples`` random sign
assignments estimates the same quantity, using the standard
``p = (b + 1) / (B + 1)`` bias-corrected estimator (never zero, matching
``scipy.stats.permutation_test``'s approximate mode).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

_EXACT_MAX_M = 20


@dataclass(frozen=True, slots=True)
class PermutationResult:
    """Result of :func:`sign_flip_test`.

    ``m_nonzero`` and ``min_attainable_p`` are load-bearing for the CI
    gate's sparse-delta disclosure (T16): they let callers warn when too few
    nonzero deltas exist for rejection to be possible at a given alpha.
    """

    p: float
    m_nonzero: int
    method: Literal["exact", "monte_carlo"]
    min_attainable_p: float


def sign_flip_test(
    deltas: Sequence[float],
    *,
    sided: Literal["one", "two"],
    n_resamples: int = 10_000,
    seed: int,
) -> PermutationResult:
    """Sign-flip permutation test over paired ``deltas`` (spec §7).

    ``sided="one"`` tests the regression direction: how extreme the observed
    mean is toward the negative side under sign-symmetry (extreme <=>
    resampled mean <= observed mean). ``sided="two"`` tests either direction
    by absolute value (extreme <=> |resampled mean| >= |observed mean|).

    Full enumeration of all ``2**m_nonzero`` sign assignments is used when
    ``m_nonzero <= 20``; above that, ``n_resamples`` seeded Monte Carlo draws
    estimate the same p-value. ``seed`` makes the Monte Carlo path
    reproducible; it is unused (but still required, for a stable call
    signature) in exact mode.
    """

    deltas_arr = np.asarray(deltas, dtype=np.float64)
    if deltas_arr.size == 0:
        raise ValueError("deltas must be non-empty")

    n_total = deltas_arr.size
    observed = float(deltas_arr.mean())
    nonzero = deltas_arr[deltas_arr != 0]
    m = nonzero.size

    if m == 0:
        # No sign to flip anywhere: the single attainable statistic is the
        # observed one (all deltas are zero), which is trivially "at least
        # as extreme" as itself.
        return PermutationResult(p=1.0, m_nonzero=0, method="exact", min_attainable_p=1.0)

    if m <= _EXACT_MAX_M:
        total = 1 << m
        bit_positions = np.arange(m, dtype=np.int64)
        bits = ((np.arange(total, dtype=np.int64)[:, None] >> bit_positions) & 1).astype(np.int8)
        signs = 1 - 2 * bits
        stats = (signs @ nonzero) / n_total
        extreme = _extreme_mask(stats, observed, sided)
        count = int(extreme.sum())
        return PermutationResult(
            p=count / total,
            m_nonzero=m,
            method="exact",
            min_attainable_p=2.0**-m,
        )

    rng = np.random.default_rng(seed)
    signs = rng.integers(0, 2, size=(n_resamples, m), dtype=np.int64) * 2 - 1
    stats = (signs @ nonzero) / n_total
    extreme = _extreme_mask(stats, observed, sided)
    b = int(extreme.sum())
    return PermutationResult(
        p=(b + 1) / (n_resamples + 1),
        m_nonzero=m,
        method="monte_carlo",
        min_attainable_p=1 / (n_resamples + 1),
    )


def _extreme_mask(
    stats: np.ndarray, observed: float, sided: Literal["one", "two"]
) -> np.ndarray:
    """Boolean mask of resampled statistics at least as extreme as ``observed``."""

    if sided == "one":
        return stats <= observed
    if sided == "two":
        return np.abs(stats) >= abs(observed)
    raise ValueError(f"sided must be 'one' or 'two', got {sided!r}")
