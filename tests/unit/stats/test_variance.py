import numpy as np
import pytest

from harness.stats.variance import variance_components


class TestRecoversSimulatedRatios:
    """between_item/between_replicate must recover the generating variances
    of a simulated item x replicate array (spec §6)."""

    def test_recovers_full_composite_ratio(self):
        rng = np.random.default_rng(42)
        n_items = 800
        n_replicates = 20
        item_sd = 3.0
        replicate_sd = 1.0

        item_effects = rng.normal(scale=item_sd, size=n_items)
        noise = rng.normal(scale=replicate_sd, size=(n_items, n_replicates))
        scores = item_effects[:, None] + noise

        result = variance_components(scores)

        assert result["between_item"] == pytest.approx(item_sd**2, rel=0.2)
        assert result["between_replicate"] == pytest.approx(replicate_sd**2, rel=0.2)

    def test_judged_fields_only_fixture_recovers_its_own_ratio(self):
        # Same function, a differently-sized/valued array standing in for the
        # judged-fields-only composite -- variance_components carries no
        # field-group logic; callers run it twice on different arrays.
        rng = np.random.default_rng(7)
        n_items = 600
        n_replicates = 12
        item_sd = 5.0
        replicate_sd = 2.0

        item_effects = rng.normal(scale=item_sd, size=n_items)
        noise = rng.normal(scale=replicate_sd, size=(n_items, n_replicates))
        judged_scores = item_effects[:, None] + noise

        result = variance_components(judged_scores)

        assert result["between_item"] == pytest.approx(item_sd**2, rel=0.2)
        assert result["between_replicate"] == pytest.approx(replicate_sd**2, rel=0.2)


class TestDefinitionLiterals:
    """Hand-computable fixtures pinning the exact literal definitions:
    between_item = variance of item means; between_replicate = mean of
    within-item variances (both population variance, ddof=0)."""

    def test_between_item_is_variance_of_item_means(self):
        scores = np.array([[1.0, 3.0], [5.0, 5.0], [2.0, 4.0]])

        result = variance_components(scores)

        assert result["between_item"] == pytest.approx(float(np.var([2.0, 5.0, 3.0])))

    def test_between_replicate_is_mean_of_within_item_variances(self):
        scores = np.array([[1.0, 3.0], [5.0, 5.0], [2.0, 4.0]])

        result = variance_components(scores)

        # per-item variances: var([1,3])=1.0, var([5,5])=0.0, var([2,4])=1.0
        assert result["between_replicate"] == pytest.approx((1.0 + 0.0 + 1.0) / 3.0)

    def test_zero_between_replicate_when_no_within_item_variation(self):
        scores = np.array([[3.0, 3.0, 3.0], [7.0, 7.0, 7.0]])

        result = variance_components(scores)

        assert result["between_replicate"] == pytest.approx(0.0)
        assert result["between_item"] > 0.0

    def test_zero_between_item_when_all_item_means_equal(self):
        scores = np.array([[1.0, 5.0], [2.0, 4.0], [3.0, 3.0]])  # every row means to 3.0

        result = variance_components(scores)

        assert result["between_item"] == pytest.approx(0.0)
        assert result["between_replicate"] > 0.0

    def test_returns_only_the_two_documented_keys(self):
        scores = np.array([[1.0, 2.0], [3.0, 4.0]])

        result = variance_components(scores)

        assert set(result.keys()) == {"between_item", "between_replicate"}


class TestInputValidation:
    def test_1d_input_raises(self):
        with pytest.raises(ValueError):
            variance_components([1.0, 2.0, 3.0])

    def test_zero_items_raises(self):
        with pytest.raises(ValueError):
            variance_components(np.empty((0, 3)))

    def test_zero_replicates_raises(self):
        with pytest.raises(ValueError):
            variance_components(np.empty((3, 0)))
