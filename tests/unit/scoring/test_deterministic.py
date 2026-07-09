from harness.schema import TicketExtraction
from harness.scoring.deterministic import normalize, score_deterministic


def _extraction(**overrides) -> TicketExtraction:
    kwargs = {
        "category": "billing",
        "priority": "normal",
        "customer_name": "Jane Doe",
        "order_id": "ORD-12345",
        "product_name": "Widget",
        "issue_summary": "Customer was double-charged for a widget.",
        "requested_action": "Refund the duplicate charge.",
    }
    kwargs.update(overrides)
    return TicketExtraction(**kwargs)


class TestNormalize:
    def test_trims_casefolds_and_collapses_whitespace(self):
        assert normalize(" Jane  DOE ") == "jane doe"

    def test_none_returns_none(self):
        assert normalize(None) is None

    def test_empty_string_does_not_become_none(self):
        assert normalize("") == ""
        assert normalize("") is not None


class TestScoreDeterministic:
    def test_all_fields_match_score_one(self):
        expected = _extraction()
        actual = _extraction()

        scores = score_deterministic(expected, actual)

        assert scores == {
            "category": 1,
            "priority": 1,
            "customer_name": 1,
            "order_id": 1,
            "product_name": 1,
        }

    def test_order_id_matches_after_normalization(self):
        # Reachable because TicketExtraction is permissive per T01: it does
        # not reject an unnormalized order_id the way GoldenExpected would.
        expected = _extraction(order_id="ORD-12345")
        actual = _extraction(order_id="ord-12345")

        scores = score_deterministic(expected, actual)

        assert scores["order_id"] == 1

    def test_customer_name_matches_after_normalization(self):
        expected = _extraction(customer_name="Jane Doe")
        actual = _extraction(customer_name=" jane  doe ")

        scores = score_deterministic(expected, actual)

        assert scores["customer_name"] == 1

    def test_none_matches_none(self):
        expected = _extraction(order_id=None)
        actual = _extraction(order_id=None)

        scores = score_deterministic(expected, actual)

        assert scores["order_id"] == 1

    def test_none_does_not_match_empty_string(self):
        expected = _extraction(customer_name=None)
        actual = _extraction(customer_name="")

        scores = score_deterministic(expected, actual)

        assert scores["customer_name"] == 0

    def test_mismatched_customer_name_scores_zero(self):
        expected = _extraction(customer_name="Jane Doe")
        actual = _extraction(customer_name="John Smith")

        scores = score_deterministic(expected, actual)

        assert scores["customer_name"] == 0

    def test_mismatched_order_id_scores_zero(self):
        expected = _extraction(order_id="ORD-12345")
        actual = _extraction(order_id="ORD-99999")

        scores = score_deterministic(expected, actual)

        assert scores["order_id"] == 0

    def test_mismatched_category_scores_zero(self):
        expected = _extraction(category="billing")
        actual = _extraction(category="shipping")

        scores = score_deterministic(expected, actual)

        assert scores["category"] == 0

    def test_mismatched_priority_scores_zero(self):
        expected = _extraction(priority="low")
        actual = _extraction(priority="urgent")

        scores = score_deterministic(expected, actual)

        assert scores["priority"] == 0

    def test_mismatched_product_name_scores_zero(self):
        expected = _extraction(product_name="Widget")
        actual = _extraction(product_name="Gadget")

        scores = score_deterministic(expected, actual)

        assert scores["product_name"] == 0

    def test_does_not_include_judge_scored_fields(self):
        expected = _extraction()
        actual = _extraction()

        scores = score_deterministic(expected, actual)

        assert "issue_summary" not in scores
        assert "requested_action" not in scores
