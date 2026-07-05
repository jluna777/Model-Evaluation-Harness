"""Pydantic schemas shared across the harness.

Two extraction schemas exist deliberately:

- ``TicketExtraction`` is candidate-facing and permissive: entity fields carry
  no format constraints because they are bound to each provider's native
  structured-output mechanism, which rejects unsupported JSON-schema
  keywords (e.g. Anthropic ``output_config.format`` / OpenAI strict
  ``json_schema``). Normalizing and comparing values is scoring's job (T02).
- ``GoldenExpected`` is reference-side and strict: ``order_id`` must already
  be in canonical form (``ORD-\\d{5}``) because reference values are
  hand-curated, not model output.

``None`` is the required "not present" encoding for optional entity fields on
both sides (spec §1).
"""

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Category(StrEnum):
    billing = "billing"
    shipping = "shipping"
    account = "account"
    product = "product"
    other = "other"


class Priority(StrEnum):
    low = "low"
    normal = "normal"
    high = "high"
    urgent = "urgent"


class EmailInput(BaseModel):
    """One customer support email (spec §1)."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    from_: str = Field(alias="from")
    subject: str
    body: str


class TicketExtraction(BaseModel):
    """Candidate-facing extraction output. Permissive: no pattern constraints."""

    model_config = ConfigDict(extra="forbid")

    category: Category
    priority: Priority
    customer_name: str | None
    order_id: str | None
    product_name: str | None
    issue_summary: str
    requested_action: str


class GoldenExpected(TicketExtraction):
    """Reference-side extraction: strict validation on top of the candidate schema."""

    order_id: str | None = Field(default=None, pattern=r"^ORD-\d{5}$")


class GoldenMeta(BaseModel):
    """Provenance and taxonomy tags for one golden/calibration item (spec §3)."""

    model_config = ConfigDict(extra="forbid")

    slice: Literal["nominal", "adversarial"]
    categories: list[str]
    difficulty: Literal[1, 2, 3]
    generator: str
    edited: bool
    notes: str = ""


class GoldenItem(BaseModel):
    """One golden/calibration dataset item (spec §3)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    email: EmailInput
    expected: GoldenExpected
    meta: GoldenMeta


class CalibrationLabel(BaseModel):
    """One owner-authored pass/fail label over a judged field (spec §5)."""

    model_config = ConfigDict(extra="forbid")

    label_id: str
    item_id: str
    candidate: Literal["a", "b"]
    field: Literal["issue_summary", "requested_action"]
    verdict: Literal["pass", "fail"]
    critique: str
    label_date: date
    round: Literal["initial", "retest"]


class Certificate(BaseModel):
    """Committed judge calibration certificate (spec §5)."""

    model_config = ConfigDict(extra="forbid")

    judge_version: str
    overall_kappa: float
    kappa_ci: tuple[float, float]
    per_candidate_kappa: dict[str, float]
    verdict: Literal["adequate", "adequate_with_caveat", "inadequate"]
    ceiling_kappa: float | None = None
    label_file_hash: str
    date: date
