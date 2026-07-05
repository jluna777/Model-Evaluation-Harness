import math

import numpy as np
import pytest
from sklearn.metrics import cohen_kappa_score

from harness.stats.agreement import KappaResult, cohens_kappa


class TestMatchesSklearn:
    """Cohen's kappa is the single deciding agreement statistic (spec §5);
    its point estimate must match sklearn's reference implementation."""

    def test_perfect_agreement_is_kappa_one(self):
        a = ["pass", "fail", "pass", "pass", "fail"] * 6
        b = list(a)

        result = cohens_kappa(a, b)

        assert result.kappa == pytest.approx(1.0)
        assert result.kappa == pytest.approx(cohen_kappa_score(a, b))

    def test_independent_labels_is_kappa_near_zero(self):
        rng = np.random.default_rng(0)
        n = 200
        a = rng.choice(["pass", "fail"], size=n, p=[0.5, 0.5]).tolist()
        b = rng.choice(["pass", "fail"], size=n, p=[0.5, 0.5]).tolist()

        result = cohens_kappa(a, b)

        assert result.kappa == pytest.approx(cohen_kappa_score(a, b), abs=1e-9)
        assert result.kappa == pytest.approx(0.0, abs=0.15)

    def test_skewed_90_10_prevalence_fixture_matches_sklearn(self):
        # Curated calibration sets skew toward "pass" (D2) -- this is the
        # regime where raw agreement misleads and kappa is load-bearing.
        rng = np.random.default_rng(1)
        n = 300
        a = rng.choice(["pass", "fail"], size=n, p=[0.9, 0.1])
        flip = rng.random(n) < 0.15
        b = np.where(flip, np.where(a == "pass", "fail", "pass"), a)

        result = cohens_kappa(a.tolist(), b.tolist())

        assert result.kappa == pytest.approx(cohen_kappa_score(a, b), abs=1e-9)
        assert result.prevalence == pytest.approx(float(np.mean(a == "pass")))


class TestDescriptiveContext:
    """raw_agreement and prevalence are descriptive only (spec §5) -- they
    never decide adequacy, but must be correct and exposed on the result."""

    def test_raw_agreement_and_prevalence_on_a_known_fixture(self):
        a = ["pass"] * 9 + ["fail"]  # 9/10 pass -> prevalence 0.9
        b = ["pass"] * 7 + ["fail"] * 3  # agrees on 7 pass + 1 fail = 8/10

        result = cohens_kappa(a, b)

        assert result.raw_agreement == pytest.approx(0.8)
        assert result.prevalence == pytest.approx(0.9)

    def test_prevalence_is_documented_as_fraction_of_a_not_b(self):
        # a is all "pass" (prevalence 1.0); b is mostly "fail". Confirms
        # prevalence reads from `a`, the reference/first-argument sequence.
        a = ["pass"] * 10
        b = ["fail"] * 8 + ["pass"] * 2

        result = cohens_kappa(a, b)

        assert result.prevalence == pytest.approx(1.0)


class TestClusterCiWidensCorrelatedData:
    """spec §5, D2: all calibration CIs cluster-bootstrap resample emails
    because judgments within one email are correlated. A cluster CI on
    perfectly-correlated within-cluster judgments must be wider than the
    naive (unclustered) CI on the exact same points."""

    @staticmethod
    def _build_fixture(seed: int, n_clusters: int = 40, per_cluster: int = 5):
        rng = np.random.default_rng(seed)
        archetypes = [("pass", "pass"), ("fail", "fail"), ("pass", "fail"), ("fail", "pass")]
        weights = [0.45, 0.30, 0.15, 0.10]
        choice = rng.choice(len(archetypes), size=n_clusters, p=weights)

        a: list[str] = []
        b: list[str] = []
        clusters: list[int] = []
        for cluster_id in range(n_clusters):
            label_a, label_b = archetypes[choice[cluster_id]]
            for _ in range(per_cluster):
                a.append(label_a)
                b.append(label_b)
                clusters.append(cluster_id)
        return a, b, clusters

    def test_cluster_ci_wider_than_naive_ci_on_perfectly_correlated_clusters(self):
        a, b, clusters = self._build_fixture(seed=1)

        naive = cohens_kappa(a, b, seed=7, n_resamples=8000)
        clustered = cohens_kappa(a, b, clusters=clusters, seed=7, n_resamples=8000)

        naive_width = naive.ci[1] - naive.ci[0]
        cluster_width = clustered.ci[1] - clustered.ci[0]

        assert not math.isnan(cluster_width)
        assert cluster_width > naive_width
        # Clustering must not change the point estimate, only the CI.
        assert clustered.kappa == pytest.approx(naive.kappa)


class TestDegenerateSingleCategory:
    """Cohen's kappa is undefined (0/0) when both sequences collapse onto a
    single shared category. Convention (documented in agreement.py, mirrors
    sklearn's own `replace_undefined_by=nan` default): return kappa=nan
    instead of raising."""

    def test_single_shared_category_returns_nan_without_crashing(self):
        a = ["pass"] * 12
        b = ["pass"] * 12

        result = cohens_kappa(a, b)

        assert math.isnan(result.kappa)
        assert result.raw_agreement == pytest.approx(1.0)

    def test_single_shared_category_with_clusters_does_not_crash(self):
        a = ["pass"] * 12
        b = ["pass"] * 12
        clusters = np.repeat(np.arange(4), 3)

        result = cohens_kappa(a, b, clusters=clusters)

        assert math.isnan(result.kappa)


class TestReproducibility:
    def test_same_seed_yields_identical_result(self):
        a = ["pass", "fail"] * 30
        b = ["pass", "pass"] * 30
        clusters = np.repeat(np.arange(20), 3)

        first = cohens_kappa(a, b, clusters=clusters, seed=5, n_resamples=1000)
        second = cohens_kappa(a, b, clusters=clusters, seed=5, n_resamples=1000)

        assert first == second


class TestResultShape:
    def test_result_is_kappa_result_with_documented_fields(self):
        a = ["pass", "fail", "pass", "fail"]
        b = ["pass", "pass", "pass", "fail"]

        result = cohens_kappa(a, b)

        assert isinstance(result, KappaResult)
        assert isinstance(result.kappa, float)
        assert isinstance(result.ci, tuple)
        assert len(result.ci) == 2
        assert isinstance(result.raw_agreement, float)
        assert isinstance(result.prevalence, float)


class TestInputValidation:
    def test_mismatched_lengths_raises(self):
        with pytest.raises(ValueError):
            cohens_kappa(["pass", "fail"], ["pass"])

    def test_too_few_observations_raises(self):
        with pytest.raises(ValueError):
            cohens_kappa(["pass"], ["fail"])
