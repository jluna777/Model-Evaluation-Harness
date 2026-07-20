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

    For two-sided tests, the ``min_attainable_p`` floor reflects a mirror-pairing
    constraint: because two-sided extremeness (|stat| >= |observed|) is invariant
    under full sign-negation of all deltas, extreme configurations always come in
    equal-magnitude pairs (positive and negative). This reduces the count of
    distinct attainable p-values; the floor is 2**(1-m) for m >= 1.
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

    ``sided`` is validated FIRST, before any enumeration/resampling work
    begins -- an invalid value used to only surface from ``_extreme_mask``,
    after the (potentially expensive, up to ``2**20`` entries) exact
    enumeration had already run to completion.
    """

    if sided not in ("one", "two"):
        raise ValueError(f"sided must be 'one' or 'two', got {sided!r}")

    deltas_arr = np.asarray(deltas, dtype=np.float64)
    if deltas_arr.size == 0:
        raise ValueError("deltas must be non-empty")

    n_total = deltas_arr.size
    nonzero = deltas_arr[deltas_arr != 0]
    m = nonzero.size
    # The observed statistic is the mean over ALL deltas (zeros included),
    # but it is computed through the SAME dot-product expression the
    # resampled statistics use below (the identity sign assignment), never
    # via ndarray.mean(): pairwise summation over the full array and a
    # dot product over the nonzero values can differ in the last ulp, and
    # that mismatch excluded the observed value's own tie block -- which
    # always contains the identity assignment -- from the extreme count
    # (final whole-branch review 2026-07-20, Critical C1: returned p-values
    # below min_attainable_p and could reject at m = 4 where the documented
    # floor makes rejection impossible).
    observed = float(np.ones(m, dtype=np.int8) @ nonzero) / n_total if m else 0.0

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
        if sided == "one":
            floor = 2.0**-m
        else:  # sided == "two"
            floor = min(1.0, 2.0**(1 - m))
        return PermutationResult(
            p=count / total,
            m_nonzero=m,
            method="exact",
            min_attainable_p=floor,
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
    """Boolean mask of resampled statistics at least as extreme as ``observed``.

    Comparisons carry a relative tolerance (the same guard
    ``scipy.stats.permutation_test`` documents for its null distribution):
    distinct sign assignments that are mathematically tied with the observed
    statistic can differ from it in the last ulp, and a strict comparison
    would drop the whole tie block from the extreme count -- the
    anti-conservative direction (final whole-branch review 2026-07-20, C1).
    The tolerance (1e-14 relative, floored absolutely at 1e-14) is
    astronomically below the gate's delta quantum (100/21/32 at K=3), so it
    can only ever merge genuine floating-point tie blocks, never distinct
    statistics."""

    tol = 1e-14 * max(1.0, abs(observed))
    if sided == "one":
        return stats <= observed + tol
    if sided == "two":
        return np.abs(stats) >= abs(observed) - tol
    raise ValueError(f"sided must be 'one' or 'two', got {sided!r}")
