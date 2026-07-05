import math
import warnings

import numpy as np
import pytest
from scipy import stats as scipy_stats

from harness.stats.bootstrap import _bca_adjust, _bca_percentiles, _norm_cdf, _norm_ppf, bca_ci


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
        # hand-picked constant: empirically the correct implementation's
        # disagreement with scipy stays around 0.01 * standard_error, while
        # a zeroed-acceleration mutant disagrees by 0.19-0.47 * standard_error
        # and a zeroed-bias-correction (z0) mutant by 0.09-0.14 *
        # standard_error on data of this shape -- so 0.15 * standard_error
        # leaves ~15x headroom over the correct implementation's noise while
        # still catching the acceleration mutant. (It cannot reliably catch
        # the z0 mutant on its own -- see TestBcaAdjustmentTerms below for a
        # deterministic, term-level test that does.)
        tol = 0.15 * scipy_result.standard_error

        assert abs(lo - scipy_lo) <= tol
        assert abs(hi - scipy_hi) <= tol


class TestBcaAdjustmentTerms:
    """Deterministic, hand-computed check of the z0/acceleration terms.

    ``TestAgreesWithScipyBca`` above only catches a broken bias-correction
    (z0) term within a fraction of the Monte Carlo noise floor and can miss
    it; this class instead feeds fixed, hand-computable inputs straight into
    the internal helpers and compares against literals derived independently
    of the module's own formula, so it kills a zeroed-``a`` or zeroed-``z0``
    mutant deterministically.

    Fixed inputs:
      - ``boot_stats = [1..10]``, ``observed = 4.5`` -> exactly 4 of the 10
        bootstrap stats (1, 2, 3, 4) are strictly less than 4.5, so
        ``p0 = 0.4`` and ``z0 = norm.ppf(0.4) = -0.2533471031357997``.
      - ``jack_stats = [1.0, 2.0, 4.0]`` -> mean ``theta_dot = 7/3``, giving
        ``d = theta_dot - jack = [4/3, 1/3, -5/3]``.
        ``sum(d**3) = (64 + 1 - 125) / 27 = -60/27 = -20/9 ≈ -2.2222222``
        ``sum(d**2) = (16 + 1 + 25) / 9 = 42/9 = 14/3 ≈ 4.6666667``
        ``a = sum(d**3) / (6 * sum(d**2)**1.5) ≈ -2.2222222 / 60.4869132
          ≈ -0.0367389``
      - ``alpha = 0.10`` (level=0.90) -> ``z_lo = norm.ppf(0.05)``,
        ``z_hi = norm.ppf(0.95)`` (``±1.6448536269514722``).

    All literals below were computed independently in a scratch script
    (``scipy.stats.norm.ppf``/``.cdf`` plus the ``_bca_adjust`` formula
    transcribed by hand) rather than by re-running the module's own helpers,
    so the assertions are not tautological.
    """

    boot_stats = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
    observed = 4.5
    jack_stats = np.array([1.0, 2.0, 4.0])
    alpha = 0.10  # level = 0.90

    z0 = -0.2533471031357997
    a = -0.03673889284811709
    z_lo = -1.6448536269514729
    z_hi = 1.6448536269514722

    def test_bca_percentiles_match_hand_computed_literals(self):
        lo_pct, hi_pct = _bca_percentiles(
            self.boot_stats, self.jack_stats, self.observed, self.alpha
        )

        assert lo_pct == pytest.approx(0.010899619829167055, abs=1e-6)
        assert hi_pct == pytest.approx(0.8577988153330023, abs=1e-6)

    def test_bca_adjust_matches_hand_computed_literals(self):
        adj_lo = _bca_adjust(self.z0, self.a, self.z_lo)
        adj_hi = _bca_adjust(self.z0, self.a, self.z_hi)

        assert adj_lo == pytest.approx(-2.293847852566169, abs=1e-6)
        assert adj_hi == pytest.approx(1.0704820834562707, abs=1e-6)

    def test_zeroed_acceleration_changes_the_adjusted_value(self):
        # Mutation check: a zeroed acceleration term must move the
        # adjusted z away from the correct (a != 0) value computed above.
        adj_lo_correct = _bca_adjust(self.z0, self.a, self.z_lo)
        adj_hi_correct = _bca_adjust(self.z0, self.a, self.z_hi)

        adj_lo_a0 = _bca_adjust(self.z0, 0.0, self.z_lo)
        adj_hi_a0 = _bca_adjust(self.z0, 0.0, self.z_hi)

        assert adj_lo_a0 == pytest.approx(-2.151547833223072, abs=1e-6)
        assert adj_hi_a0 == pytest.approx(1.1381594206798729, abs=1e-6)
        assert adj_lo_a0 != pytest.approx(adj_lo_correct, abs=1e-6)
        assert adj_hi_a0 != pytest.approx(adj_hi_correct, abs=1e-6)

    def test_zeroed_bias_correction_changes_the_adjusted_value(self):
        # Mutation check: a zeroed z0 term must move the adjusted z away
        # from the correct (z0 != 0) value computed above.
        adj_lo_correct = _bca_adjust(self.z0, self.a, self.z_lo)
        adj_hi_correct = _bca_adjust(self.z0, self.a, self.z_hi)

        adj_lo_z00 = _bca_adjust(0.0, self.a, self.z_lo)
        adj_hi_z00 = _bca_adjust(0.0, self.a, self.z_hi)

        assert adj_lo_z00 == pytest.approx(-1.7506452994792383, abs=1e-6)
        assert adj_hi_z00 == pytest.approx(1.55111932900198, abs=1e-6)
        assert adj_lo_z00 != pytest.approx(adj_lo_correct, abs=1e-6)
        assert adj_hi_z00 != pytest.approx(adj_hi_correct, abs=1e-6)


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


def _nan_above_threshold(threshold: float):
    """Statistic that returns NaN whenever the (resampled) mean exceeds
    ``threshold``, else the mean itself. Engineered so that, on the fixed
    data/seed used below, a known fraction of bootstrap replicates come back
    NaN while the observed statistic and every jackknife leave-one-out
    statistic stay finite -- isolating the "some but not all replicates are
    NaN" case from degenerate-observed or degenerate-jackknife cases.
    """

    def statistic(x: np.ndarray) -> float:
        m = float(np.mean(x))
        return float("nan") if m > threshold else m

    return statistic


def _nan_on_duplicate(x: np.ndarray) -> float:
    """Statistic that returns NaN whenever ``x`` contains a repeated value,
    else the mean. Bootstrap resamples draw ``n`` values with replacement
    from ``n`` distinct values, so for even a moderate ``n`` a resample with
    *no* repeats is astronomically unlikely (~n!/n^n) -- every bootstrap
    replicate comes back NaN in practice, deterministically, for any seed.
    Jackknife leave-one-out subsets never contain duplicates (they are drawn
    without replacement from already-distinct values), so this isolates the
    "all bootstrap replicates NaN, jackknife untouched" case.
    """

    if len(x) != len(np.unique(x)):
        return float("nan")
    return float(np.mean(x))


class TestNanPolicy:
    """`bca_ci`'s NaN handling: a single NaN bootstrap replicate must not
    silently flow into `np.percentile` and produce a silent `(nan, nan)`
    interval (the bug this module is being hardened against -- see
    `cohens_kappa`'s documented degenerate single-category convention,
    which is a real-world source of NaN replicates). Default policy fails
    loudly; `nan_policy="omit"` recovers a finite CI with disclosure.
    """

    # Fixed data/seed combination (verified empirically, see the docstrings
    # on the engineered statistics above): with threshold=0.35, exactly 172
    # of 10_000 bootstrap replicates are NaN and 0 of 20 jackknife values
    # are NaN, for this values/seed pair.
    _values = np.random.default_rng(42).normal(loc=0.0, scale=1.0, size=20)
    _seed = 123
    _n_resamples = 10_000
    _threshold = 0.35

    def test_default_policy_raises_with_nan_count_in_message(self):
        statistic = _nan_above_threshold(self._threshold)

        with pytest.raises(ValueError, match="172"):
            bca_ci(
                self._values,
                statistic,
                level=0.95,
                seed=self._seed,
                n_resamples=self._n_resamples,
            )

    def test_default_policy_message_mentions_omit_suggestion(self):
        statistic = _nan_above_threshold(self._threshold)

        with pytest.raises(ValueError, match="nan_policy"):
            bca_ci(
                self._values,
                statistic,
                level=0.95,
                seed=self._seed,
                n_resamples=self._n_resamples,
            )

    def test_omit_policy_returns_finite_interval_within_sane_bounds(self):
        statistic = _nan_above_threshold(self._threshold)

        with pytest.warns(RuntimeWarning):
            lo, hi = bca_ci(
                self._values,
                statistic,
                level=0.95,
                seed=self._seed,
                n_resamples=self._n_resamples,
                nan_policy="omit",
            )

        assert math.isfinite(lo)
        assert math.isfinite(hi)
        assert lo <= hi
        # Sane bounds: the underlying data is standard-normal-ish (mean ~0,
        # std ~0.85, n=20), so a 95% CI on the mean has no business landing
        # outside a generous +/-3 band.
        assert -3.0 < lo < 3.0
        assert -3.0 < hi < 3.0

    def test_omit_policy_warning_names_the_omission_count(self):
        statistic = _nan_above_threshold(self._threshold)

        with pytest.warns(RuntimeWarning, match=r"172 of 10000"):
            bca_ci(
                self._values,
                statistic,
                level=0.95,
                seed=self._seed,
                n_resamples=self._n_resamples,
                nan_policy="omit",
            )

    def test_all_nan_bootstrap_replicates_raises_even_under_omit(self):
        values = np.random.default_rng(7).normal(loc=0.0, scale=1.0, size=20)

        with pytest.raises(ValueError):
            bca_ci(
                values,
                _nan_on_duplicate,
                level=0.95,
                seed=1,
                n_resamples=500,
                nan_policy="omit",
            )

    def test_all_nan_bootstrap_replicates_also_raises_under_default_policy(self):
        values = np.random.default_rng(7).normal(loc=0.0, scale=1.0, size=20)

        with pytest.raises(ValueError):
            bca_ci(values, _nan_on_duplicate, level=0.95, seed=1, n_resamples=500)

    def test_observed_statistic_nan_always_raises_regardless_of_policy(self):
        with pytest.raises(ValueError):
            bca_ci(
                [1.0, 2.0, 3.0, 4.0],
                lambda x: float("nan"),
                level=0.95,
                seed=1,
                n_resamples=200,
                nan_policy="omit",
            )

    def test_existing_no_nan_path_emits_no_warning(self):
        # Default (no-NaN-engineered) usage must not warn at all -- promote
        # any warning to an error so a regression that starts warning on the
        # happy path fails loudly here.
        rng = np.random.default_rng(21)
        values = rng.normal(size=30)

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            lo, hi = bca_ci(values, np.mean, level=0.95, seed=1, n_resamples=2000)

        assert lo <= hi
