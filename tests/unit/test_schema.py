import pytest
from pydantic import ValidationError

from harness.schema import (
    CalibrationLabel,
    Certificate,
    EmailInput,
    GoldenItem,
    TicketExtraction,
)


def _valid_extraction_kwargs(**overrides):
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
    return kwargs


def _valid_email_kwargs():
    return {"from": "a@example.com", "subject": "Help", "body": "My order never arrived."}


class TestEmailInput:
    def test_accepts_from_alias(self):
        email = EmailInput(**_valid_email_kwargs())
        assert email.from_ == "a@example.com"
        assert email.subject == "Help"
        assert email.body == "My order never arrived."


class TestTicketExtractionPermissive:
    def test_accepts_lowercase_unnormalized_order_id(self):
        # Normalization is scoring's job (T02) -- the candidate-facing model
        # must not reject an order_id that fails the reference-side pattern.
        extraction = TicketExtraction(**_valid_extraction_kwargs(order_id="ord-12345"))
        assert extraction.order_id == "ord-12345"

    def test_accepts_none_for_optional_entity_fields(self):
        extraction = TicketExtraction(
            **_valid_extraction_kwargs(customer_name=None, order_id=None, product_name=None)
        )
        assert extraction.customer_name is None
        assert extraction.order_id is None
        assert extraction.product_name is None

    def test_rejects_uppercase_priority_enum_value(self):
        with pytest.raises(ValidationError):
            TicketExtraction(**_valid_extraction_kwargs(priority="URGENT"))

    def test_rejects_unknown_category(self):
        with pytest.raises(ValidationError):
            TicketExtraction(**_valid_extraction_kwargs(category="refunds"))

    def test_json_schema_has_no_pattern_keyword(self):
        # The schema is bound to provider structured-output APIs, which reject
        # unsupported JSON-schema keywords such as `pattern`.
        schema = TicketExtraction.model_json_schema()
        assert "pattern" not in _flatten_schema(schema)


def _flatten_schema(node) -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        keys.update(node.keys())
        for value in node.values():
            keys.update(_flatten_schema(value))
    elif isinstance(node, list):
        for item in node:
            keys.update(_flatten_schema(item))
    return keys


class TestGoldenItemStrictReference:
    def test_accepts_valid_order_id_pattern(self):
        item = GoldenItem(
            id="golden-001",
            email=_valid_email_kwargs(),
            expected=_valid_extraction_kwargs(order_id="ORD-12345"),
            meta={
                "slice": "nominal",
                "categories": ["billing"],
                "difficulty": 1,
                "generator": "gpt-5.4-mini",
                "edited": False,
                "notes": "",
            },
        )
        assert item.expected.order_id == "ORD-12345"

    def test_rejects_unnormalized_order_id(self):
        with pytest.raises(ValidationError):
            GoldenItem(
                id="golden-002",
                email=_valid_email_kwargs(),
                expected=_valid_extraction_kwargs(order_id="ord-12345"),
                meta={
                    "slice": "nominal",
                    "categories": ["billing"],
                    "difficulty": 1,
                    "generator": "gpt-5.4-mini",
                    "edited": False,
                    "notes": "",
                },
            )

    def test_rejects_none_order_id_is_still_allowed(self):
        # None means "not present" and must remain valid on the reference side too.
        item = GoldenItem(
            id="golden-003",
            email=_valid_email_kwargs(),
            expected=_valid_extraction_kwargs(order_id=None),
            meta={
                "slice": "adversarial",
                "categories": ["shipping"],
                "difficulty": 2,
                "generator": "claude-haiku-4-5-20251001",
                "edited": True,
                "notes": "curated",
            },
        )
        assert item.expected.order_id is None


class TestCalibrationLabel:
    def test_round_trips(self):
        label = CalibrationLabel(
            label_id="lbl-001",
            item_id="cal-001",
            candidate="a",
            field="issue_summary",
            verdict="pass",
            critique="Matches the reference intent.",
            label_date="2026-06-01",
            round="initial",
            output_sha256="a" * 64,
        )
        assert label.verdict == "pass"
        assert label.round == "initial"
        assert label.output_sha256 == "a" * 64

    def test_output_sha256_is_required(self):
        with pytest.raises(ValidationError):
            CalibrationLabel(
                label_id="lbl-001",
                item_id="cal-001",
                candidate="a",
                field="issue_summary",
                verdict="pass",
                critique="Matches the reference intent.",
                label_date="2026-06-01",
                round="initial",
            )


class TestCertificate:
    def test_round_trips(self):
        certificate = Certificate(
            judge_version="deadbeef",
            overall_kappa=0.72,
            kappa_ci=(0.55, 0.85),
            per_candidate_kappa={"a": 0.7, "b": 0.74},
            verdict="adequate",
            ceiling_kappa=None,
            label_file_hash="sha256:abc123",
            date="2026-07-04",
        )
        assert certificate.verdict == "adequate"
        assert certificate.kappa_ci == (0.55, 0.85)

    def test_per_candidate_kappa_ci_defaults_to_none(self):
        # Additive T14 field: absent input -> None, reproducing the original
        # CI-less shape exactly (no behavior change for pre-T14 certificates).
        certificate = Certificate(
            judge_version="deadbeef",
            overall_kappa=0.72,
            kappa_ci=(0.55, 0.85),
            per_candidate_kappa={"a": 0.7, "b": 0.74},
            verdict="adequate",
            label_file_hash="sha256:abc123",
            date="2026-07-04",
        )
        assert certificate.per_candidate_kappa_ci is None

    def test_per_candidate_kappa_ci_round_trips_when_present(self):
        certificate = Certificate(
            judge_version="deadbeef",
            overall_kappa=0.72,
            kappa_ci=(0.55, 0.85),
            per_candidate_kappa={"a": 0.7, "b": 0.74},
            per_candidate_kappa_ci={"a": (0.5, 0.85), "b": (0.55, 0.9)},
            verdict="adequate",
            label_file_hash="sha256:abc123",
            date="2026-07-04",
        )
        assert certificate.per_candidate_kappa_ci == {"a": (0.5, 0.85), "b": (0.55, 0.9)}
