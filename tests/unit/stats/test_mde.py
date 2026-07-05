import math

import pytest

from harness.stats.mde import mde


class TestOneSidedAnchor:
    """spec §7/D3: MDE must use the gate's one-sided z, not a two-sided one."""

    def test_mde_12_32_matches_one_sided_anchor(self):
        assert mde(12.0, 32) == pytest.approx(5.27, abs=0.01)

    def test_two_sided_quantile_would_overshoot_and_must_not_be_produced(self):
        # The foil: swapping the one-sided z_alpha (1.645, i.e. norm.ppf(0.95))
        # for the two-sided one (1.960, norm.ppf(0.975)) at the same n/delta_sd
        # gives ~5.94, not ~5.27. This documents that mde() must not produce
        # the two-sided value.
        result = mde(12.0, 32)

        assert result == pytest.approx(5.27, abs=0.01)
        assert result != pytest.approx(5.94, abs=0.01)


class TestFormula:
    def test_matches_hand_computed_one_sided_formula(self):
        # Literals computed independently (scipy.stats.norm.ppf), not by
        # re-running mde()'s own internals.
        z_alpha = 1.6448536269514722  # norm.ppf(1 - 0.05), one-sided
        z_beta = 0.8416212335729143  # norm.ppf(0.80)
        expected = (z_alpha + z_beta) * 12.0 / math.sqrt(32)

        assert mde(12.0, 32) == pytest.approx(expected, abs=1e-6)

    def test_larger_n_gives_smaller_mde(self):
        assert mde(12.0, 128) < mde(12.0, 32)

    def test_larger_delta_sd_gives_larger_mde(self):
        assert mde(24.0, 32) > mde(12.0, 32)

    def test_zero_delta_sd_gives_zero_mde(self):
        assert mde(0.0, 32) == pytest.approx(0.0)

    def test_higher_power_requires_larger_mde(self):
        assert mde(12.0, 32, power=0.90) > mde(12.0, 32, power=0.80)

    def test_lower_alpha_requires_larger_mde(self):
        assert mde(12.0, 32, alpha=0.01) > mde(12.0, 32, alpha=0.05)


class TestInputValidation:
    def test_zero_n_raises(self):
        with pytest.raises(ValueError):
            mde(12.0, 0)

    def test_negative_n_raises(self):
        with pytest.raises(ValueError):
            mde(12.0, -5)

    def test_negative_delta_sd_raises(self):
        with pytest.raises(ValueError):
            mde(-1.0, 32)

    def test_alpha_zero_raises(self):
        with pytest.raises(ValueError):
            mde(12.0, 32, alpha=0.0)

    def test_alpha_one_raises(self):
        with pytest.raises(ValueError):
            mde(12.0, 32, alpha=1.0)

    def test_power_zero_raises(self):
        with pytest.raises(ValueError):
            mde(12.0, 32, power=0.0)

    def test_power_one_raises(self):
        with pytest.raises(ValueError):
            mde(12.0, 32, power=1.0)
