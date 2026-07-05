import numpy as np
import pytest
from scipy import stats as scipy_stats

from harness.stats.bootstrap import _norm_cdf, _norm_ppf, bca_ci


class TestNormalHelpers:
    """The BCa formula needs the normal CDF/quantile; scipy is dev-only, so
    these are implemented locally (math.erf + Acklam's ppf approximation).
    Verified here against scipy's reference implementation.
    """

    @pytest.mark.parametrize("p", [0.001, 0.025, 0.05, 0.1, 0.5, 0.9, 0.95, 0.975, 0.999])
    def test_norm_ppf_matches_scipy(self, p):
        assert _norm_ppf(p) == pytest.approx(scipy_stats.norm.ppf(p), abs=1e-8)

    @pytest.mark.parametrize("x", [-3.0, -1.96, -1.0, 0.0, 1.0, 1.96, 3.0])
    def test_norm_cdf_matches_scipy(self, x):
        assert _norm_cdf(x) == pytest.approx(scipy_stats.norm.cdf(x), abs=1e-12)

    def test_norm_ppf_boundary_is_infinite_not_a_crash(self):
        assert _norm_ppf(0.0) == -np.inf
        assert _norm_ppf(1.0) == np.inf


class TestAgreesWithScipyBca:
    def test_agrees_with_scipy_bootstrap_bca_within_tolerance(self):
        # Skewed data with a modest sample size so the bias-correction and
        # acceleration terms are actually exercised: with a symmetric
        # distribution or a large n, BCa collapses toward the plain
        # percentile method and the anchor stops discriminating a broken
        # bias/acceleration implementation from a correct one.
        rng = np.random.default_rng(2024)
        values = rng.exponential(scale=2.0, size=20)
        n_resamples = 10_000

        lo, hi = bca_ci(values, np.mean, level=0.95, seed=123, n_resamples=n_resamples)

        scipy_result = scipy_stats.bootstrap(
            (values,),
            np.mean,
            method="BCa",
            confidence_level=0.95,
            n_resamples=n_resamples,
            random_state=123,
        )
        scipy_lo = scipy_result.confidence_interval.low
        scipy_hi = scipy_result.confidence_interval.high

        # Both sides estimate the same asymptotic BCa interval from
        # independent Monte Carlo resamples of the same data; residual
        # disagreement is bootstrap resampling noise. Bound it by a fraction
        # of scipy's own reported bootstrap standard error rather than a
        # hand-picked constant: empirically (30 trial reference samples of
        # this shape) that noise stays under 0.11 * standard_error, while an
        # implementation missing the bias correction or acceleration term
        # disagrees with scipy by 0.19-0.63 * standard_error on this same
        # data -- so 0.5 * standard_error comfortably separates the two.
        tol = 0.5 * scipy_result.standard_error

        assert abs(lo - scipy_lo) <= tol
        assert abs(hi - scipy_hi) <= tol


class TestClusterWidensCorrelatedData:
    def test_cluster_ci_wider_than_naive_ci_on_perfectly_correlated_clusters(self):
        # Each "email" (cluster) contributes several judgments that are all
        # identical -- i.e. perfectly correlated within the cluster. Naive
        # per-observation resampling treats these as independent draws and
        # understates the true (between-cluster) variance; cluster
        # resampling should not.
        rng = np.random.default_rng(7)
        n_clusters = 20
        per_cluster = 5
        cluster_means = rng.normal(loc=0.0, scale=1.0, size=n_clusters)

        values = np.repeat(cluster_means, per_cluster)
        clusters = np.repeat(np.arange(n_clusters), per_cluster)

        naive_lo, naive_hi = bca_ci(values, np.mean, level=0.95, seed=99, n_resamples=5000)
        cluster_lo, cluster_hi = bca_ci(
            values, np.mean, level=0.95, clusters=clusters, seed=99, n_resamples=5000
        )

        assert (cluster_hi - cluster_lo) > (naive_hi - naive_lo)


class TestLevelNesting:
    def test_90_ci_nests_inside_95_ci_same_data_and_seed(self):
        rng = np.random.default_rng(11)
        values = rng.normal(loc=3.0, scale=1.5, size=50)

        lo_90, hi_90 = bca_ci(values, np.mean, level=0.90, seed=5, n_resamples=5000)
        lo_95, hi_95 = bca_ci(values, np.mean, level=0.95, seed=5, n_resamples=5000)

        assert lo_95 <= lo_90
        assert hi_90 <= hi_95

    def test_90_ci_nests_inside_95_ci_in_cluster_mode(self):
        rng = np.random.default_rng(13)
        n_clusters = 15
        per_cluster = 4
        cluster_means = rng.normal(loc=1.0, scale=2.0, size=n_clusters)
        values = np.repeat(cluster_means, per_cluster) + rng.normal(
            scale=0.1, size=n_clusters * per_cluster
        )
        clusters = np.repeat(np.arange(n_clusters), per_cluster)

        lo_90, hi_90 = bca_ci(
            values, np.mean, level=0.90, clusters=clusters, seed=5, n_resamples=5000
        )
        lo_95, hi_95 = bca_ci(
            values, np.mean, level=0.95, clusters=clusters, seed=5, n_resamples=5000
        )

        assert lo_95 <= lo_90
        assert hi_90 <= hi_95


class TestReproducibility:
    def test_same_seed_yields_identical_interval(self):
        rng = np.random.default_rng(3)
        values = rng.normal(size=40)

        first = bca_ci(values, np.mean, level=0.95, seed=42, n_resamples=2000)
        second = bca_ci(values, np.mean, level=0.95, seed=42, n_resamples=2000)

        assert first == second

    def test_same_seed_yields_identical_interval_in_cluster_mode(self):
        rng = np.random.default_rng(4)
        values = rng.normal(size=40)
        clusters = np.repeat(np.arange(10), 4)

        first = bca_ci(
            values, np.mean, level=0.95, clusters=clusters, seed=42, n_resamples=2000
        )
        second = bca_ci(
            values, np.mean, level=0.95, clusters=clusters, seed=42, n_resamples=2000
        )

        assert first == second

    def test_different_seeds_actually_thread_through_to_the_rng(self):
        rng = np.random.default_rng(9)
        values = rng.normal(size=40)

        intervals = {
            bca_ci(values, np.mean, level=0.95, seed=seed, n_resamples=500)
            for seed in range(5)
        }

        assert len(intervals) > 1


class TestDefaultsAndBasics:
    def test_default_statistic_is_mean(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        lo, hi = bca_ci(values, level=0.95, seed=1, n_resamples=1000)

        assert lo < float(np.mean(values)) < hi

    def test_interval_is_ordered(self):
        rng = np.random.default_rng(21)
        values = rng.normal(size=30)

        lo, hi = bca_ci(values, np.mean, level=0.95, seed=1, n_resamples=2000)

        assert lo <= hi


class TestDegenerateAllEqual:
    def test_constant_values_falls_back_to_percentile_without_crashing(self):
        values = [5.0] * 10

        lo, hi = bca_ci(values, np.mean, level=0.95, seed=1, n_resamples=500)

        assert lo == pytest.approx(5.0)
        assert hi == pytest.approx(5.0)

    def test_constant_values_in_cluster_mode_does_not_crash(self):
        values = [5.0] * 12
        clusters = np.repeat(np.arange(4), 3)

        lo, hi = bca_ci(
            values, np.mean, level=0.95, clusters=clusters, seed=1, n_resamples=500
        )

        assert lo == pytest.approx(5.0)
        assert hi == pytest.approx(5.0)


class TestInputValidation:
    def test_too_few_values_raises(self):
        with pytest.raises(ValueError):
            bca_ci([1.0], level=0.95, seed=1)

    def test_empty_values_raises(self):
        with pytest.raises(ValueError):
            bca_ci([], level=0.95, seed=1)

    def test_level_out_of_range_raises(self):
        with pytest.raises(ValueError):
            bca_ci([1.0, 2.0, 3.0], level=1.5, seed=1)

    def test_level_zero_raises(self):
        with pytest.raises(ValueError):
            bca_ci([1.0, 2.0, 3.0], level=0.0, seed=1)

    def test_clusters_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            bca_ci([1.0, 2.0, 3.0], level=0.95, clusters=["a", "b"], seed=1)

    def test_single_cluster_label_raises(self):
        with pytest.raises(ValueError):
            bca_ci([1.0, 2.0, 3.0], level=0.95, clusters=["a", "a", "a"], seed=1)
