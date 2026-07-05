import pytest

from harness.scoring.composite import CompositeMode, composite

_ALL_PASSING = {
    "category": 1,
    "priority": 1,
    "customer_name": 1,
    "order_id": 1,
    "product_name": 1,
    "issue_summary": 1,
    "requested_action": 1,
}


class TestCompositeMode:
    def test_member_names_are_load_bearing(self):
        # Consumed verbatim by T15/T16 gate code and T01's fingerprint
        # composite_mode argument -- these names must not drift.
        assert CompositeMode.FULL_7.name == "FULL_7"
        assert CompositeMode.DETERMINISTIC_5.name == "DETERMINISTIC_5"


class TestComposite:
    def test_all_fields_passing_full_7_is_100(self):
        assert composite(_ALL_PASSING, CompositeMode.FULL_7) == 100.0

    def test_all_fields_passing_deterministic_5_is_100(self):
        assert composite(_ALL_PASSING, CompositeMode.DETERMINISTIC_5) == 100.0

    def test_full_7_and_deterministic_5_differ_on_judge_field_failures(self):
        field_scores = dict(_ALL_PASSING, issue_summary=0, requested_action=0)

        full = composite(field_scores, CompositeMode.FULL_7)
        deterministic = composite(field_scores, CompositeMode.DETERMINISTIC_5)

        assert full != deterministic
        assert deterministic == 100.0
        assert full == pytest.approx(5 / 7 * 100)

    def test_deterministic_5_ignores_judge_field_scores(self):
        field_scores = dict(_ALL_PASSING, category=0)

        assert composite(field_scores, CompositeMode.DETERMINISTIC_5) == pytest.approx(4 / 5 * 100)

    def test_unweighted_mean_over_mixed_scores(self):
        field_scores = dict(_ALL_PASSING, category=0, priority=0, issue_summary=0)

        assert composite(field_scores, CompositeMode.FULL_7) == pytest.approx(4 / 7 * 100)
