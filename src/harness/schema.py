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

    order_id: str | None = Field(pattern=r"^ORD-\d{5}$")


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
    """One annotator-authored pass/fail label over a judged field (spec §5,
    dual-annotation upgrade amended 2026-07-09, owner).

    ``annotator`` (REQUIRED): a free-string identifier for who wrote this
    label -- ``"owner"`` is always the primary annotator and the one who
    adjudicates disagreements; any other string identifies the second,
    independent annotator (e.g. ``"annotator2"``). Deliberately not a
    ``Literal`` so the second annotator's identity is a labeling-time choice,
    not a schema constant -- ``harness.calibrate`` only ever hardcodes the
    ``"owner"`` value (``OWNER_ANNOTATOR``) and treats whichever other
    annotator string appears as "the second annotator".

    ``round`` is ``"initial"`` (either annotator's first-pass label on a
    judged field) or ``"adjudication"`` (the OWNER's tie-break verdict on a
    field the two annotators' ``"initial"`` rounds disagreed on -- see
    ``harness.calibrate.resolve_gold_labels``). The original single-annotator
    design's ``"retest"`` round (intra-annotator test-retest) is retired: no
    ``CalibrationLabel`` ever persisted one (spec/D2 amendment 2026-07-09), so
    the literal simply no longer allows it.

    ``output_sha256`` (REQUIRED, T14 finding F1 -- binding): the sha256 hex
    digest of the EXACT ``candidate_value`` string the annotator was looking
    at when they wrote this label. The exact normalization is: the raw
    string, encoded UTF-8, with NO trimming or other normalization of any
    kind (not even leading/trailing whitespace) -- see ``harness.calibrate.
    hash_output``, the one function that must be used to produce this value.

    This field exists because a label otherwise pairs to a judged triple by
    ``(item_id, candidate, field)`` alone, which is silently wrong the moment
    the run directory that produced that triple's candidate output is
    regenerated -- e.g. temp>0 candidate-model nondeterminism, and
    ``results/`` is gitignored so regeneration is the ordinary case, not an
    edge case. Without this hash, a relabeled-looking key could actually be
    paired against a DIFFERENT candidate output than the one the annotator
    labeled, silently corrupting the agreement statistic. Binding now applies
    across every annotator and round: ``harness.calibrate.
    _verify_dual_annotator_coverage`` checks it between the two annotators'
    ``"initial"`` rows, ``resolve_gold_labels`` checks it again against any
    ``"adjudication"`` row, and ``pair_with_labels``/``pair_judgments_with_
    labels`` recompute it from each reconstructed triple's live
    ``candidate_value`` before any pairing proceeds (all-or-nothing,
    ``CalibrationBindingError`` otherwise) -- the same precedent throughout.
    """

    model_config = ConfigDict(extra="forbid")

    label_id: str
    item_id: str
    candidate: Literal["a", "b"]
    field: Literal["issue_summary", "requested_action"]
    annotator: str
    verdict: Literal["pass", "fail"]
    critique: str
    label_date: date
    round: Literal["initial", "adjudication"]
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
    ``_certificate_section``).

    ``ceiling_kappa``/``ceiling_kappa_ci`` (semantics amended 2026-07-09,
    owner -- dual-annotation upgrade): originally the single annotator's
    test-retest (intra-annotator) consistency estimate. Now Cohen's kappa
    between the TWO independent annotators' verdicts over the doubly-labeled
    calibration set -- the inter-annotator agreement (IAA) ceiling
    (``harness.calibrate.compute_iaa_ceiling``), with its own cluster-
    bootstrap CI (``ceiling_kappa_ci``, additive). The decision semantics are
    unchanged: a judge kappa exceeding this ceiling indicates estimation
    noise, not a super-human judge. ``ceiling_kappa_ci`` is ``None`` for any
    certificate produced before this field existed (the same additive
    convention as ``per_candidate_kappa_ci``).

    ``n_adjudicated`` (additive, dual-annotation upgrade): the count of
    judged fields where the two annotators' initial verdicts disagreed and
    the OWNER's adjudication (round=``"adjudication"``) supplied the final
    gold verdict (``harness.calibrate.resolve_gold_labels``) -- an honest
    disclosure of how much of the gold set required a tie-break, rather than
    reflecting spontaneous agreement. ``None`` for any certificate produced
    before dual annotation (the single-annotator design had no concept of
    adjudication at all)."""

    model_config = ConfigDict(extra="forbid")

    judge_version: str
    overall_kappa: float
    kappa_ci: tuple[float, float]
    per_candidate_kappa: dict[str, float]
    per_candidate_kappa_ci: dict[str, tuple[float, float]] | None = None
    verdict: Literal["adequate", "adequate_with_caveat", "inadequate"]
    ceiling_kappa: float | None = None
    ceiling_kappa_ci: tuple[float, float] | None = None
    n_adjudicated: int | None = None
    label_file_hash: str
    date: date
