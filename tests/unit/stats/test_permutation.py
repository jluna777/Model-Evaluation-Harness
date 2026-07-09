from typing import Literal, cast

import numpy as np
import pytest
from scipy import stats as scipy_stats

from harness.stats.permutation import sign_flip_test


class TestExactModeAnchors:
    def test_m2_all_negative_deltas_one_sided_p_is_quarter(self):
        result = sign_flip_test([-1.0, -2.0], sided="one", seed=0)

        assert result.p == pytest.approx(0.25)
        assert result.m_nonzero == 2
        assert result.method == "exact"
        assert result.min_attainable_p == pytest.approx(0.25)

    def test_m5_all_negative_deltas_one_sided_p_is_one_over_32(self):
        deltas = [-1.0, -2.0, -3.0, -4.0, -5.0]
        result = sign_flip_test(deltas, sided="one", seed=0)

        assert result.p == pytest.approx(0.03125)
        assert result.m_nonzero == 5
        assert result.method == "exact"
        assert result.min_attainable_p == pytest.approx(0.03125)

    def test_all_zero_deltas_p_is_one_and_m_nonzero_is_zero(self):
        result = sign_flip_test([0.0, 0.0, 0.0, 0.0, 0.0], sided="one", seed=0)

        assert result.p == 1.0
        assert result.m_nonzero == 0
        assert result.method == "exact"
        assert result.min_attainable_p == 1.0

    def test_zero_deltas_excluded_from_m_nonzero_but_dilute_the_mean(self):
        # Mirrors the m=2 all-negative anchor but with a zero delta mixed
        # in: m_nonzero stays 2 (only nonzero deltas get sign-flipped) while
        # the statistic (mean over ALL deltas) reflects all three values.
        result = sign_flip_test([-1.0, -2.0, 0.0], sided="one", seed=0)

        assert result.p == pytest.approx(0.25)
        assert result.m_nonzero == 2
        assert result.min_attainable_p == pytest.approx(0.25)

    def test_single_nonzero_delta_is_the_m1_boundary(self):
        # m=1: only two sign assignments exist; a negative delta is the
        # more extreme of the two (ties impossible for a single value).
        result = sign_flip_test([-3.0], sided="one", seed=0)

        assert result.p == pytest.approx(0.5)
        assert result.m_nonzero == 1
        assert result.min_attainable_p == pytest.approx(0.5)


class TestTwoSided:
    def test_two_sided_p_is_double_one_sided_for_a_clean_extreme(self):
        deltas = [-5.0, -1.0]

        one_sided = sign_flip_test(deltas, sided="one", seed=0)
        two_sided = sign_flip_test(deltas, sided="two", seed=0)

        assert one_sided.p == pytest.approx(0.25)
        assert two_sided.p == pytest.approx(0.5)

    def test_two_sided_extremeness_uses_absolute_value(self):
        # All-positive mirror of the m=5 anchor: the identity sign pattern
        # is the unique maximum of the null distribution, so the one-sided
        # (regression-direction) p-value is trivially 1.0 -- every resampled
        # mean is <= the observed one. But that same identity value is also
        # the largest in absolute value (tied only with its full negation),
        # so the two-sided p-value correctly flags it as extreme.
        deltas = [1.0, 2.0, 3.0, 4.0, 5.0]

        one_sided = sign_flip_test(deltas, sided="one", seed=0)
        two_sided = sign_flip_test(deltas, sided="two", seed=0)

        assert one_sided.p == pytest.approx(1.0)
        assert two_sided.p == pytest.approx(2 / 32)


class TestExactMonteCarloBoundary:
    def test_m_equal_20_is_exact(self):
        deltas = [-(i + 1.0) for i in range(20)]
        result = sign_flip_test(deltas, sided="one", seed=0)

        assert result.m_nonzero == 20
        assert result.method == "exact"
        assert result.min_attainable_p == pytest.approx(2.0**-20)

    def test_m_equal_21_is_monte_carlo(self):
        deltas = [-(i + 1.0) for i in range(21)]
        result = sign_flip_test(deltas, sided="one", n_resamples=500, seed=0)

        assert result.m_nonzero == 21
        assert result.method == "monte_carlo"
        assert result.min_attainable_p == pytest.approx(1 / 501)


class TestMonteCarloReproducibility:
    def test_same_seed_yields_identical_p(self):
        rng = np.random.default_rng(7)
        deltas = rng.normal(loc=-0.3, scale=1.0, size=30)

        first = sign_flip_test(deltas, sided="one", n_resamples=1000, seed=42)
        second = sign_flip_test(deltas, sided="one", n_resamples=1000, seed=42)

        assert first.p == second.p
        assert first.method == "monte_carlo"

    def test_different_seeds_are_actually_threaded_through_to_the_rng(self):
        # Zero-mean, equal-magnitude deltas put the observed statistic near
        # the center of the null distribution rather than at a floor/ceiling
        # extreme, so p-values across distinct seeds land all over (0, 1)
        # instead of piling up at a shared boundary value. Collecting results
        # across 10 seeds and requiring more than one distinct value confirms
        # `seed` actually drives the draw (not silently ignored) without
        # asserting on any single non-deterministic comparison.
        deltas = [0.5, -0.5] * 15

        ps = {
            sign_flip_test(deltas, sided="one", n_resamples=1000, seed=seed).p
            for seed in range(10)
        }

        assert len(ps) > 1


class TestMonteCarloAgreesWithScipy:
    def test_agrees_with_scipy_permutation_test_within_tolerance(self):
        rng = np.random.default_rng(2024)
        deltas = rng.normal(loc=-0.3, scale=1.0, size=40)
        n_resamples = 10_000

        ours = sign_flip_test(deltas, sided="one", n_resamples=n_resamples, seed=123)

        scipy_result = scipy_stats.permutation_test(
            (deltas,),
            np.mean,
            permutation_type="samples",
            alternative="less",
            n_resamples=n_resamples,
            random_state=123,
        )

        assert ours.m_nonzero == 40
        assert ours.method == "monte_carlo"

        p = ours.p
        tolerance = 4 * np.sqrt(p * (1 - p) / n_resamples)
        assert abs(ours.p - scipy_result.pvalue) <= tolerance


class TestMinAttainableP:
    def test_exact_mode_min_attainable_p_is_two_to_the_minus_m(self):
        result = sign_flip_test([-1.0, -2.0, -3.0], sided="one", seed=0)

        assert result.min_attainable_p == pytest.approx(2.0**-3)

    def test_monte_carlo_mode_min_attainable_p_is_one_over_b_plus_one(self):
        deltas = [-(i + 1.0) for i in range(25)]
        result = sign_flip_test(deltas, sided="one", n_resamples=999, seed=0)

        assert result.min_attainable_p == pytest.approx(1 / 1000)

    def test_two_sided_m1_min_attainable_p_is_one(self):
        # For m=1, the mirror-pairing constraint forces a floor of 1.0 (both
        # configurations are equidistant extremes and tie).
        result = sign_flip_test([-3.0], sided="two", seed=0)

        assert result.p == pytest.approx(1.0)
        assert result.min_attainable_p == pytest.approx(1.0)

    def test_two_sided_m3_min_attainable_p_is_floor_of_mirror_pairing(self):
        # For m=3, clearly-extreme all-negative deltas. The mirror-pairing
        # constraint gives a floor of min(1.0, 2.0**(1-3)) = min(1.0, 0.25) = 0.25.
        # All three sign flips to positive is also extreme and equally rare.
        deltas = [-5.0, -6.0, -7.0]
        result = sign_flip_test(deltas, sided="two", seed=0)

        assert result.p == pytest.approx(0.25)
        assert result.min_attainable_p == pytest.approx(0.25)

    def test_two_sided_m5_min_attainable_p_is_floor_of_mirror_pairing(self):
        # For m=5, all-negative deltas. Mirror-pairing floor is 2.0**(1-5) = 0.0625.
        deltas = [-1.0, -2.0, -3.0, -4.0, -5.0]
        result = sign_flip_test(deltas, sided="two", seed=0)

        assert result.min_attainable_p == pytest.approx(0.0625)
        assert result.p == pytest.approx(0.0625)

    def test_one_sided_m5_min_attainable_p_unchanged(self):
        # One-sided floor remains 2.0**-m regardless.
        deltas = [-1.0, -2.0, -3.0, -4.0, -5.0]
        result = sign_flip_test(deltas, sided="one", seed=0)

        assert result.min_attainable_p == pytest.approx(0.03125)  # 2**-5


class TestInputValidation:
    def test_empty_deltas_raises(self):
        with pytest.raises(ValueError):
            sign_flip_test([], sided="one", seed=0)

    def test_invalid_sided_raises_before_any_enumeration(self):
        # `sided` is validated at the very top, before enumeration/resampling
        # -- a large-but-valid `deltas` here would be expensive to enumerate
        # if the (bogus) value slipped past validation and only failed deep
        # inside `_extreme_mask`. cast() bypasses the Literal type check so
        # the runtime validation itself is exercised.
        bogus_sided = cast(Literal["one", "two"], "bogus")

        with pytest.raises(ValueError, match="sided"):
            sign_flip_test([-1.0, -2.0, -3.0], sided=bogus_sided, seed=0)
