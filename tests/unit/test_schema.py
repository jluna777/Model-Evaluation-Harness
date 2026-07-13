import pytest
from pydantic import ValidationError

from harness.schema import (
    CalibrationLabel,
    Certificate,
    EmailInput,
    GoldenItem,
    PerturbationOverlay,
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
            annotator="owner",
            verdict="pass",
            critique="Matches the reference intent.",
            label_date="2026-06-01",
            round="initial",
            output_sha256="a" * 64,
        )
        assert label.verdict == "pass"
        assert label.round == "initial"
        assert label.annotator == "owner"
        assert label.output_sha256 == "a" * 64

    def test_adjudication_round_by_owner_round_trips(self):
        # Dual-annotation upgrade (2026-07-09): the retired "retest" round is
        # replaced by "adjudication" -- always by the owner.
        label = CalibrationLabel(
            label_id="lbl-002",
            item_id="cal-001",
            candidate="a",
            field="issue_summary",
            annotator="owner",
            verdict="fail",
            critique="Tie-break: missing an essential detail.",
            label_date="2026-06-02",
            round="adjudication",
            output_sha256="b" * 64,
        )
        assert label.round == "adjudication"

    def test_second_annotator_is_a_free_string(self):
        label = CalibrationLabel(
            label_id="lbl-003",
            item_id="cal-001",
            candidate="a",
            field="issue_summary",
            annotator="annotator2",
            verdict="pass",
            critique="Looks right.",
            label_date="2026-06-01",
            round="initial",
            output_sha256="c" * 64,
        )
        assert label.annotator == "annotator2"

    def test_retest_round_no_longer_allowed(self):
        with pytest.raises(ValidationError):
            CalibrationLabel(
                label_id="lbl-004",
                item_id="cal-001",
                candidate="a",
                field="issue_summary",
                annotator="owner",
                verdict="pass",
                critique="x",
                label_date="2026-06-01",
                round="retest",
                output_sha256="d" * 64,
            )

    def test_annotator_is_required(self):
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
                output_sha256="a" * 64,
            )

    def test_output_sha256_is_required(self):
        with pytest.raises(ValidationError):
            CalibrationLabel(
                label_id="lbl-001",
                item_id="cal-001",
                candidate="a",
                field="issue_summary",
                annotator="owner",
                verdict="pass",
                critique="Matches the reference intent.",
                label_date="2026-06-01",
                round="initial",
            )


class TestPerturbationOverlay:
    def test_round_trips_with_valid_corruption_type(self):
        row = PerturbationOverlay(
            item_id="cal-101",
            candidate="a",
            field="issue_summary",
            perturbed_value="Customer wants a full refund immediately.",
            corruption_type="ungrounded_addition",
            rationale="Adds a refund request never mentioned in the email.",
        )
        assert row.item_id == "cal-101"
        assert row.candidate == "a"
        assert row.field == "issue_summary"
        assert row.corruption_type == "ungrounded_addition"

    @pytest.mark.parametrize(
        "corruption_type",
        [
            "dropped_essential",
            "ungrounded_addition",
            "contradiction",
            "supersession_leak",
            "entity_swap",
        ],
    )
    def test_every_enum_value_accepted(self, corruption_type):
        row = PerturbationOverlay(
            item_id="cal-101",
            candidate="b",
            field="requested_action",
            perturbed_value="perturbed text",
            corruption_type=corruption_type,
            rationale="probe",
        )
        assert row.corruption_type == corruption_type

    def test_rejects_unknown_corruption_type(self):
        with pytest.raises(ValidationError):
            PerturbationOverlay(
                item_id="cal-101",
                candidate="a",
                field="issue_summary",
                perturbed_value="x",
                corruption_type="made_up_type",
                rationale="probe",
            )

    def test_rejects_unknown_field(self):
        with pytest.raises(ValidationError):
            PerturbationOverlay(
                item_id="cal-101",
                candidate="a",
                field="customer_name",
                perturbed_value="x",
                corruption_type="entity_swap",
                rationale="probe",
            )

    def test_rejects_unknown_candidate(self):
        with pytest.raises(ValidationError):
            PerturbationOverlay(
                item_id="cal-101",
                candidate="c",
                field="issue_summary",
                perturbed_value="x",
                corruption_type="entity_swap",
                rationale="probe",
            )

    def test_extra_field_forbidden(self):
        with pytest.raises(ValidationError):
            PerturbationOverlay(
                item_id="cal-101",
                candidate="a",
                field="issue_summary",
                perturbed_value="x",
                corruption_type="entity_swap",
                rationale="probe",
                extra_field="not allowed",
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

    def test_ceiling_kappa_ci_and_n_adjudicated_default_to_none(self):
        # Additive dual-annotation-upgrade fields (2026-07-09): absent input
        # -> None, reproducing the pre-upgrade certificate shape exactly.
        certificate = Certificate(
            judge_version="deadbeef",
            overall_kappa=0.72,
            kappa_ci=(0.55, 0.85),
            per_candidate_kappa={"a": 0.7, "b": 0.74},
            verdict="adequate",
            label_file_hash="sha256:abc123",
            date="2026-07-04",
        )
        assert certificate.ceiling_kappa_ci is None
        assert certificate.n_adjudicated is None

    def test_ceiling_kappa_ci_and_n_adjudicated_round_trip_when_present(self):
        certificate = Certificate(
            judge_version="deadbeef",
            overall_kappa=0.72,
            kappa_ci=(0.55, 0.85),
            per_candidate_kappa={"a": 0.7, "b": 0.74},
            verdict="adequate",
            ceiling_kappa=0.85,
            ceiling_kappa_ci=(0.7, 0.95),
            n_adjudicated=3,
            label_file_hash="sha256:abc123",
            date="2026-07-04",
        )
        assert certificate.ceiling_kappa == pytest.approx(0.85)
        assert certificate.ceiling_kappa_ci == (0.7, 0.95)
        assert certificate.n_adjudicated == 3

    def test_perturbation_fields_default_to_none(self):
        # Additive fail-probe fields (D2 amendment 2026-07-10): absent input
        # -> None, reproducing the pre-amendment certificate shape exactly.
        certificate = Certificate(
            judge_version="deadbeef",
            overall_kappa=0.72,
            kappa_ci=(0.55, 0.85),
            per_candidate_kappa={"a": 0.7, "b": 0.74},
            verdict="adequate",
            label_file_hash="sha256:abc123",
            date="2026-07-04",
        )
        assert certificate.n_perturbed is None
        assert certificate.achieved_fail_prevalence is None
        assert certificate.real_only_kappa is None
        assert certificate.real_only_kappa_ci is None
        assert certificate.perturbed_rows_passed_by_gold is None

    def test_perturbation_fields_round_trip_when_present(self):
        certificate = Certificate(
            judge_version="deadbeef",
            overall_kappa=0.72,
            kappa_ci=(0.55, 0.85),
            per_candidate_kappa={"a": 0.7, "b": 0.74},
            verdict="adequate",
            label_file_hash="sha256:abc123",
            date="2026-07-04",
            n_perturbed=6,
            achieved_fail_prevalence=0.24,
            real_only_kappa=0.68,
            real_only_kappa_ci=(0.4, 0.85),
            perturbed_rows_passed_by_gold=1,
        )
        assert certificate.n_perturbed == 6
        assert certificate.achieved_fail_prevalence == pytest.approx(0.24)
        assert certificate.real_only_kappa == pytest.approx(0.68)
        assert certificate.real_only_kappa_ci == (0.4, 0.85)
        assert certificate.perturbed_rows_passed_by_gold == 1
