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
    """One owner-authored pass/fail label over a judged field (spec §5).

    ``output_sha256`` (REQUIRED, T14 finding F1 -- binding): the sha256 hex
    digest of the EXACT ``candidate_value`` string the owner was looking at
    when they wrote this label. The exact normalization is: the raw string,
    encoded UTF-8, with NO trimming or other normalization of any kind (not
    even leading/trailing whitespace) -- see ``harness.calibrate.hash_output``,
    the one function that must be used to produce this value.

    This field exists because a label otherwise pairs to a judged triple by
    ``(item_id, candidate, field)`` alone, which is silently wrong the moment
    the run directory that produced that triple's candidate output is
    regenerated -- e.g. temp>0 candidate-model nondeterminism, and
    ``results/`` is gitignored so regeneration is the ordinary case, not an
    edge case. Without this hash, a relabeled-looking key could actually be
    paired against a DIFFERENT candidate output than the one the owner
    labeled, silently corrupting the agreement statistic. ``harness.
    calibrate.pair_with_labels`` recomputes this hash from each reconstructed
    triple's live ``candidate_value`` and requires an exact match before any
    pairing proceeds (all-or-nothing, ``CalibrationBindingError`` otherwise).
    """

    model_config = ConfigDict(extra="forbid")

    label_id: str
    item_id: str
    candidate: Literal["a", "b"]
    field: Literal["issue_summary", "requested_action"]
    verdict: Literal["pass", "fail"]
    critique: str
    label_date: date
    round: Literal["initial", "retest"]
    output_sha256: str


class Certificate(BaseModel):
    """Committed judge calibration certificate (spec §5).

    ``per_candidate_kappa_ci`` (additive, T14): per-candidate cluster-bootstrap
    CIs, keyed the same as ``per_candidate_kappa``. T01 only stored per-candidate
    kappa *point estimates* (``kappa_ci`` covers the overall kappa only) even
    though spec §5's report header prose calls for "κ ± CI per candidate" -- a
    ledgered gap from T10's review. ``None`` (the default) reproduces that
    original, CI-less shape exactly for any certificate that predates this
    field; ``reports.py``'s rendering is unchanged for that case (see
    ``_certificate_section``)."""

    model_config = ConfigDict(extra="forbid")

    judge_version: str
    overall_kappa: float
    kappa_ci: tuple[float, float]
    per_candidate_kappa: dict[str, float]
    per_candidate_kappa_ci: dict[str, tuple[float, float]] | None = None
    verdict: Literal["adequate", "adequate_with_caveat", "inadequate"]
    ceiling_kappa: float | None = None
    label_file_hash: str
    date: date
