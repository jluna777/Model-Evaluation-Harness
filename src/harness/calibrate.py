"""Judge calibration: agreement report, certificate, self-consistency (spec
§5, §4; ticket T14).

**Candidate-outputs-for-calibration design (binding, read before touching
this module):** this module does NOT invent a parallel pipeline for
generating candidate outputs over the calibration emails. Instead it reuses
T08's ``run_eval``/``load_run`` machinery exactly the way ``eval run``/
``eval compare`` already do: the calibration procedure runs each candidate
over ``data/calibration/emails.jsonl`` via the ordinary ``run_eval`` path
(``cli.py``'s ``calibrate`` command drives this through the same
``_get_or_run`` seam ``run``/``compare`` use), producing one persisted
``RunArtifact`` per candidate. ``build_triples`` then *reconstructs* the
``(email, reference, candidate_value)`` triples this module needs straight
from that artifact's own persisted rows: ``email``/``expected`` come from the
artifact's embedded ``GoldenItem``s (persisted verbatim at run time, spec
AC5), and ``candidate_value`` comes from parsing that item's persisted
``raw_output`` JSON. No candidate call is ever made here -- only the JUDGE is
re-invoked (``judge_triples``, ``measure_self_consistency``), against
already-produced candidate output, which is exactly what "the judge is run
over the (email, reference, candidate_value) triples reconstructed from
persisted candidate outputs" means operationally.

Calibration runs are expected at K=1 (spec §5's 25 x 2 fields x 2 candidates
= 100 field judgments -- one candidate output per item per field, not K
replicates of it; ``CalibrationLabel`` itself carries no replicate index).
``build_triples`` tolerates K>1 defensively by always taking the
lowest-``replicate`` row per item, but production calibration runs should be
generated with ``k=1``.

Re-judging (rather than trusting the candidate run's own embedded
``field_scores``, which ``run_eval`` already computed via ``Judge.judge_field``
at run time) is deliberate: calibration must certify the CURRENT judge
(``judge_version()``), and re-invoking it here -- through the exact same
``Judge.judge_field`` call every production judged field goes through -- is
what lets this module assert its own ``judge_version()`` on the certificate
without trusting that the candidate run's manifest was produced under an
identical judge (a run and a calibration certification are not guaranteed to
happen atomically with each other).

**Statistics (spec §5, D2):** Cohen's kappa (``harness.stats.agreement.
cohens_kappa``) is the single agreement statistic, computed overall and per
candidate, always with a cluster-bootstrap CI resampling emails (judgments of
one email -- both candidates, both fields -- move together). Raw agreement
and label prevalence are carried through as descriptive context only, never
a decision input. Adequacy is decided on the OVERALL kappa point estimate
(``decide_verdict``); a per-candidate kappa gap above 0.2 is a flag for D1
review only (``per_candidate_divergence_flag``), never a gate condition.

**Self-consistency (spec §4):** 20 fixed ``(email, reference,
candidate-value)`` triples -- a deterministic prefix of the full,
sorted-by-``(item_id, candidate, field)`` triple set, so the same calibration
data always selects the same 20 -- are each judged 3x; the flip rate (the
fraction of the 20 for which the 3 judgments disagree) is reported and
carried into the certificate context.

**Dual-annotation IAA ceiling and gold resolution (owner-approved upgrade,
2026-07-09 -- replaces single-annotator test-retest, constitution §6 D2):**
BOTH annotators (``"owner"``, the primary annotator, plus a second,
independent annotator identified by whatever other ``annotator`` string
appears) independently label ALL ``round="initial"`` field judgments from
their own hash-bound labeling sheet (``labeling_template_rows``, per
annotator) -- neither sees the other's verdicts.
``_verify_dual_annotator_coverage`` is the shared precondition both
downstream functions build on: exactly two annotators, one of them
``OWNER_ANNOTATOR``, labeling the EXACT same set of ``(item_id, candidate,
field)`` keys (no partial-intersection tolerance -- an incomplete second
annotator undermines the whole premise) with matching ``output_sha256`` per
shared key (``CalibrationBindingError`` otherwise, the retest-binding
precedent extended to two annotators). ``compute_iaa_ceiling`` computes
Cohen's kappa between the two annotators' verdicts over that doubly-labeled
set, with its own cluster-bootstrap CI -- *the human-human agreement
ceiling*, surfaced as ``ceiling_kappa``/``ceiling_kappa_ci`` on the
certificate; the decision semantics are unchanged from the retired ceiling: a
judge kappa exceeding it is estimation noise, not a super-human judge.
``resolve_gold_labels`` derives the FINAL gold verdict per key -- the
owner's verdict where the two annotators agree, or the OWNER's adjudication
verdict (``round="adjudication"``, ``annotator="owner"``) where they
disagree, raising loudly (``DualAnnotationError``) and naming every key if a
disagreement has no adjudication row. Judge agreement/kappa is computed
against this resolved gold, never against either annotator's raw label
directly. ``Certificate.n_adjudicated`` discloses how many gold labels came
from adjudication rather than spontaneous agreement. Before any of that,
``_validate_adjudication_round`` checks the WHOLE adjudication round up
front and raises ``DualAnnotationError`` naming every offending row for any
of: a non-owner adjudication row, an adjudication row on a key the two
annotators already agree on, or an adjudication row keyed outside the shared
initial key set -- each previously either silently ignored or silently
discarded rather than raising. Gold resolution (and therefore this
validation) runs before the judge is ever invoked in ``run_calibration``,
since none of it depends on a live judge call.

**Population parity (owner-ruled correction, 2026-07-09):** every number on
the certificate -- judge kappa (overall and per-candidate), the ceiling
kappa + its CI (``compute_iaa_ceiling``), and ``Certificate.n_adjudicated``
-- is computed over exactly ONE population: the paired, validly-judged key
set. ``_pair_entries`` (shared by ``pair_with_labels``/``pair_judgments_
with_labels``) enforces this right after its output-binding check (finding
F1): a gold label with no corresponding judgment, or a judged key labeled by
neither annotator, each raise ``DualAnnotationError`` naming every
offending key -- this REPLACES the old ``unlabeled_excluded`` tolerance,
under which a judged-but-unlabeled key was silently excluded from judge
kappa while the ceiling kept computing over the full doubly-labeled set
regardless. That population mismatch is exactly the kind of drift output-
hash binding cannot catch: divergence removes a key from the paired set
rather than corrupting a shared one, so it never trips the F1 check. A
judge error (``verdict is None``) remains the one tolerated gap -- the
judge failing on a key is not a label-file bug -- now excluded from the
ceiling kappa and ``n_adjudicated`` as well as judge kappa (previously only
the latter), so the key set behind judge kappa and the key set behind
ceiling kappa/``n_adjudicated`` are identical by construction after these
checks run. Live (``run_calibration``): the population-parity checks run
after judging, since they depend on the judgment set; the labels-only
validations above still run first. Offline (``run_calibration_offline``):
every input is already on disk, so every check runs before any statistic is
computed.

**Bootstrap omission disclosure (D2/agreement.py):** ``cohens_kappa``'s CI
path may emit exactly one ``RuntimeWarning`` disclosing how many degenerate
bootstrap resamples were omitted (see ``stats/agreement.py``'s module
docstring). Every kappa call in this module is wrapped to capture such
warnings via ``warnings.catch_warnings(record=True)`` rather than letting
them propagate to the caller's default warning handling, and the captured
messages are rendered verbatim in the calibration report's "Bootstrap
Disclosures" section -- silently swallowing them would hide exactly the
disclosure spec/D2 requires.

**Certificate (schema.py ``Certificate``, committed as
``data/calibration/certificate.json``):** ``build_certificate`` populates
every spec §5 field, including the additive ``per_candidate_kappa_ci`` (T14
closing a ledgered T01/T10 gap -- see ``schema.py``'s ``Certificate``
docstring). ``date`` defaults to the most recent ``label_date`` across all
labels (any round) unless an explicit override is supplied -- never
wall-clock-dependent, so tests and re-runs against the same label file always
produce the same certificate date.

**Label-to-output binding (finding F1, extended to every annotator/round by
the dual-annotation upgrade):** a label only names a triple by ``(item_id,
candidate, field)`` -- it says nothing about WHICH candidate output was
labeled. Since candidate runs are not guaranteed deterministic (temp>0) and
``results/`` is gitignored (a run directory can be regenerated at any time),
that key alone can silently rebind a label to a DIFFERENT candidate output
than the one an annotator actually looked at. Every ``CalibrationLabel``
therefore carries ``output_sha256`` -- the sha256 of the exact
``candidate_value`` string labeled (``hash_output``) -- checked at three
points, all all-or-nothing and all naming every offending key: between the
two annotators' ``"initial"`` rows and against any ``"adjudication"`` row
(``_verify_dual_annotator_coverage``/``resolve_gold_labels``), and again
between the resolved GOLD label and the live/persisted candidate output
actually being judged now (``pair_with_labels``/``pair_judgments_with_
labels``) -- never a silent partial exclusion, which would hide exactly the
kind of corruption this check exists to catch.

**Fail-probe perturbation set (D2 amendment 2026-07-10, owner-approved):**
the owner's initial fail rate on the real 100 calibration judgments is 0% at
prompt v4 -- Cohen's kappa degenerates without fail prevalence, and spec
§5's >=20% floor cannot be reached by harder REAL emails alone (evidence:
the stratification loop already tried). The fix is a SECOND, independent
item source -- ``data/calibration/emails-fail-probe.jsonl`` (~10 items, same
``GoldenItem`` schema, ids continuing the ``cal-0NN`` sequence, never
touching the original ``emails.jsonl``) -- run through the exact same
``build_triples``/``_get_or_run`` seam at k=1, PLUS a committed overlay file
(``data/calibration/perturbations.jsonl``, ``PerturbationOverlay`` rows) that
replaces specific (probe item, candidate, field) keys' real candidate output
with a deliberately corrupted value. ``validate_perturbation_overlay``
enforces the overlay's binding invariant -- every key must reference a probe
item and an actually-judgeable (item_id, candidate, field) triple, never the
original emails file, never a typo'd/nonexistent key, never a duplicate --
all-or-nothing, naming every offending key, mirroring
``_validate_adjudication_round``'s convention. ``apply_perturbation_overlay``
then substitutes the overlaid value onto the reconstructed ``Triple`` BEFORE
anything downstream ever sees it: the same overlaid ``Triple.candidate_value``
flows into ``labeling_template_rows`` (so a labeling sheet generated from
overlaid triples is correctly hash-bound to the CORRUPTED text, never the
real output), into every label's ``output_sha256`` binding check
(``pair_with_labels``), and into ``judge_triples`` (the judge judges the
corrupted text). Rows with no overlay entry keep the real run output
unchanged -- a probe item is not required to carry a planted fail.

Statistics (spec §5, superseding its fail-enrichment paragraph for this
path): probe items are independent new emails that cluster by their own
``item_id`` exactly like any other email -- NO special clustering, no
separate population. ``run_calibration``/``run_calibration_offline``
concatenate probe triples with the real ones and let every existing
dual-annotation mechanism (gold resolution, judge agreement, the IAA
ceiling, population parity) run over the UNION unchanged. Disclosure-only
additions, all ``None`` when no fail-probe set was used (``CalibrationResult.
n_perturbed``/``achieved_fail_prevalence``/``real_only_kappa``/
``perturbed_rows_passed_by_gold``, surfaced on ``Certificate`` identically):
the count of overlaid rows, the achieved fail prevalence of the resolved
gold over the FULL combined population (the number that satisfies the >=20%
floor for this path), a real-only Cohen's kappa restricted to non-probe
items (``compute_real_only_kappa``) reported ALONGSIDE -- never replacing --
the primary overall kappa (which stays the full, probe-included population
and the sole adequacy-decision statistic), and the count of overlaid rows
whose resolved gold verdict is nonetheless ``"pass"`` (a perturbation the
human standard did not flag -- legitimate gold either way, disclosed rather
than hidden).

``real_only_kappa`` is itself ``None`` -- distinct from the "no fail-probe
set used" ``None`` above -- whenever the non-probe subset is single-category
(commit 471a41a review, Critical finding): this is the EXPECTED production
state (the real items never fail, which is exactly why the fail-probe set
exists in the first place), not a corner case. Cohen's kappa is undefined
(0/0) for a single-shared-category sample by ``harness.stats.agreement.
cohens_kappa``'s documented convention, and ``compute_real_only_kappa``
detects this up front and returns ``None`` rather than let ``harness.stats.
bootstrap.bca_ci``'s observed-NaN ``ValueError`` escape uncaught.
``build_certificate`` stores ``null``, and ``render_calibration_report``
renders an explicit "undefined" disclosure line in the Perturbation Probe
Set section rather than silently omitting the real-only κ bullet.

Offline parity: ``JudgmentRecord`` carries a persisted ``is_probe`` flag
(default ``False``, so every pre-existing ``judgments.jsonl`` round-trips
unchanged) set from the live run's own probe-item membership
(``judgment_records_from_judged``'s ``probe_item_ids`` argument) --
``run_calibration_offline`` derives its own probe-item and valid-probe-key
sets straight from that flag, needing no ``RunArtifact``/candidate items of
any kind, so it can re-validate the SAME overlay file and reproduce the
SAME disclosure numbers with zero API calls, exactly like every other
statistic this module recomputes offline.

**Zero-API recomputability (finding F2):** a live run persists every judge
call it makes -- one row per judged triple plus every self-consistency
repeat -- to ``data/calibration/judgments.jsonl`` (``write_judgments_jsonl``,
written atomically: temp file + ``os.replace``, mirroring ``runner.py``'s
``_repair_truncated_tail`` precedent). ``run_calibration_offline`` then
recomputes the FULL report + certificate from that file plus ``labels.jsonl``
with ZERO ``Judge``/``ModelClient`` construction -- spec AC5's zero-API
recompute, extended from ``eval rescore`` (which recomputes a run's score
from persisted candidate output) to calibration (recomputing the agreement
statistics from persisted judge output). It fails loudly rather than
recompute against a certificate the data no longer supports: a
``judge_version`` mismatch against the CURRENT judge (``StaleJudgmentsError``
-- stale judgments must not certify today's judge) or an ``output_sha256``
mismatch against a label (``CalibrationBindingError``, the same F1 check,
against the persisted hash rather than a freshly recomputed one since no
candidate output is available offline to re-hash).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import warnings
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from harness.judge.judge import Judge
from harness.judge.rubric import judge_version as compute_judge_version
from harness.runner import RunArtifact
from harness.schema import (
    CalibrationLabel,
    Certificate,
    EmailInput,
    GoldenItem,
    PerturbationOverlay,
)
from harness.scoring.composite import JUDGED_FIELDS
from harness.stats.agreement import KappaResult, cohens_kappa

# Dual-annotation upgrade (owner, 2026-07-09): the primary annotator, who
# also adjudicates every disagreement. The second annotator is whatever other
# ``annotator`` string appears in round="initial" labels (free string, spec §5).
OWNER_ANNOTATOR = "owner"

# Spec §5 pinned parameters -- changing these needs a dated decision-log amendment.
ADEQUACY_KAPPA_THRESHOLD = 0.6
GRAY_ZONE_CI_LOWER_THRESHOLD = 0.4
DIVERGENCE_GAP_THRESHOLD = 0.2
STRATIFICATION_FAIL_RATE_THRESHOLD = 0.20

# Spec §4: 20 fixed triples, judged 3x each.
DEFAULT_SELF_CONSISTENCY_N = 20
DEFAULT_SELF_CONSISTENCY_REPEATS = 3

DEFAULT_CI_LEVEL = 0.95
DEFAULT_N_RESAMPLES = 10_000


# --------------------------------------------------------------------------
# Triple reconstruction (module docstring's candidate-outputs design).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Triple:
    """One ``(email, reference, candidate_value)`` triple for one judged
    field of one candidate's output on one calibration item -- reconstructed
    from a persisted ``RunArtifact`` (module docstring), never freshly
    generated by this module."""

    item_id: str
    candidate: Literal["a", "b"]
    field: str
    email: EmailInput
    reference: str
    candidate_value: str


def build_triples(candidate: Literal["a", "b"], run_artifact: RunArtifact) -> list[Triple]:
    """Reconstructs every judgeable ``Triple`` for ``candidate`` from
    ``run_artifact``'s persisted items/rows, in deterministic ``item_id``
    order.

    Uses the lowest-``replicate`` row per item (calibration runs are expected
    at K=1 -- module docstring); an item whose candidate row is missing, or
    whose ``raw_output`` does not parse as a JSON object carrying the judged
    field (a schema-invalid/refusal candidate failure, which never gets a
    judge call in ``run_eval`` either), contributes no triples for that item.
    """

    triples: list[Triple] = []
    for item in sorted(run_artifact.items, key=lambda i: i.id):
        rows = run_artifact.rows_for_item(item.id)
        if not rows:
            continue
        row = min(rows, key=lambda r: r.replicate)
        try:
            output = json.loads(row.raw_output)
        except json.JSONDecodeError:
            continue
        if not isinstance(output, dict):
            continue
        for field in JUDGED_FIELDS:
            if field not in output:
                continue
            reference = str(getattr(item.expected, field))
            candidate_value = str(output[field])
            triples.append(
                Triple(
                    item_id=item.id,
                    candidate=candidate,
                    field=field,
                    email=item.email,
                    reference=reference,
                    candidate_value=candidate_value,
                )
            )
    return triples


# --------------------------------------------------------------------------
# Judging triples (re-invokes the current judge -- module docstring).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgedTriple:
    """One ``Triple`` plus the current judge's fresh verdict on it. ``verdict
    is None`` iff ``error`` is set (``Judge.judge_field``'s own error-vs-fail
    convention, spec §7: a judge error is never coerced to ``"fail"``)."""

    triple: Triple
    verdict: Literal["pass", "fail"] | None
    error: str | None
    rationale: str | None


def judge_triples(judge: Judge, triples: Sequence[Triple]) -> list[JudgedTriple]:
    """Judges every triple exactly once via ``Judge.judge_field``, preserving
    input order."""

    judged: list[JudgedTriple] = []
    for t in triples:
        result = judge.judge_field(t.email, t.field, t.reference, t.candidate_value)
        judged.append(
            JudgedTriple(
                triple=t, verdict=result.verdict, error=result.error, rationale=result.rationale
            )
        )
    return judged


# --------------------------------------------------------------------------
# Pairing judged triples with owner labels (finding F1: output binding).
# --------------------------------------------------------------------------


def hash_output(candidate_value: str) -> str:
    """sha256 hex digest of the EXACT ``candidate_value`` string an owner
    labeled (finding F1): the raw string, encoded UTF-8, with NO trimming or
    other normalization of any kind -- not even leading/trailing whitespace.
    This is the one function that must produce ``CalibrationLabel.
    output_sha256``; ``pair_with_labels``/``pair_judgments_with_labels``
    require an exact match against it (or against a previously-computed copy
    of it, offline) before any pairing proceeds."""

    return hashlib.sha256(candidate_value.encode("utf-8")).hexdigest()


class CalibrationBindingError(Exception):
    """Raised when one or more labels' recorded ``output_sha256`` do not
    match the candidate output they are keyed to (finding F1) -- live, that
    means ``hash_output`` of the reconstructed triple's live
    ``candidate_value`` (``pair_with_labels``); offline, it means the
    ``output_sha256`` persisted in ``judgments.jsonl`` at judging time
    (``pair_judgments_with_labels``, finding F2). Either way the underlying
    cause is the same: the run directory that produced this candidate output
    was regenerated (temp>0 candidate nondeterminism; ``results/`` is
    gitignored, so this is the ordinary long-run failure mode, not a freak
    accident) since the label was written, and the label's verdict no longer
    describes the output actually being judged.

    All-or-nothing: raised only after every judged item has been checked,
    naming EVERY mismatched ``(item_id, candidate, field)`` key and the total
    count -- pairing never proceeds partially. Silently excluding just the
    mismatched keys (the way a judge-error triple is excluded, the one
    tolerated gap -- population-parity invariant, 2026-07-09) would hide
    exactly the kind of silent misalignment this check exists to catch, so
    it is a hard failure instead.
    """

    def __init__(self, mismatches: Sequence[tuple[str, str, str]]) -> None:
        keys = ", ".join(str(key) for key in mismatches)
        super().__init__(
            f"{len(mismatches)} calibration label(s) do not match the candidate output they "
            "are keyed to (output_sha256 mismatch) -- the run directory was likely "
            "regenerated since labeling (temp>0 nondeterminism; results/ is gitignored). "
            f"Mismatched (item_id, candidate, field) keys: {keys}"
        )
        self.mismatches = tuple(mismatches)


class DualAnnotationError(Exception):
    """Raised when ``labels`` does not satisfy the dual-annotation design's
    core precondition (owner-approved upgrade, 2026-07-09, constitution §6
    D2): exactly two annotators with ``round="initial"`` labels, one of them
    ``OWNER_ANNOTATOR``, both covering the EXACT same set of ``(item_id,
    candidate, field)`` keys, and -- for ``resolve_gold_labels`` -- every
    disagreement between them backed by an owner adjudication row. Also
    raised by ``_pair_entries`` (population-parity invariant, owner-ruled
    2026-07-09, module docstring) when a gold label has no corresponding
    judgment, or a judged key was labeled by neither annotator -- the SAME
    "the labels file and the rest of the data must describe the exact same
    keys" family of check, just against the judgment set instead of the
    second annotator's rows. Never partially resolved: every message names
    every offending key so the labels file can be fixed directly, mirroring
    ``CalibrationBindingError``'s all-or-nothing precedent. The CLI maps this
    to a clean exit 1 (``_clean_exit_on_expected_errors``), never a
    traceback."""


def _initial_labels_by_annotator(
    labels: Sequence[CalibrationLabel],
) -> dict[str, dict[tuple[str, str, str], CalibrationLabel]]:
    """``{annotator: {(item_id, candidate, field): CalibrationLabel}}`` over
    every ``round="initial"`` label. Raises ``ValueError`` on a duplicate key
    within one annotator's own rows -- a data integrity problem (two labels
    from the same annotator for the same judged field), never silently
    resolved by picking one."""

    by_annotator: dict[str, dict[tuple[str, str, str], CalibrationLabel]] = {}
    for label in labels:
        if label.round != "initial":
            continue
        by_key = by_annotator.setdefault(label.annotator, {})
        key = (label.item_id, label.candidate, label.field)
        if key in by_key:
            raise ValueError(
                f"duplicate 'initial' label from annotator {label.annotator!r} for {key} "
                "in labels.jsonl"
            )
        by_key[key] = label
    return by_annotator


def _labels_by_annotator_round(
    labels: Sequence[CalibrationLabel], annotator: str, round_: Literal["initial", "adjudication"]
) -> dict[tuple[str, str, str], CalibrationLabel]:
    """``{(item_id, candidate, field): CalibrationLabel}`` for one
    ``(annotator, round_)`` pair. Raises ``ValueError`` on a duplicate key --
    same data-integrity convention as ``_initial_labels_by_annotator``."""

    by_key: dict[tuple[str, str, str], CalibrationLabel] = {}
    for label in labels:
        if label.round != round_ or label.annotator != annotator:
            continue
        key = (label.item_id, label.candidate, label.field)
        if key in by_key:
            raise ValueError(
                f"duplicate {round_!r} label from annotator {annotator!r} for {key} in "
                "labels.jsonl"
            )
        by_key[key] = label
    return by_key


@dataclass(frozen=True)
class DualAnnotatorCoverage:
    """The verified precondition both ``compute_iaa_ceiling`` and
    ``resolve_gold_labels`` build on -- see ``_verify_dual_annotator_coverage``."""

    owner: str
    other: str
    owner_by_key: dict[tuple[str, str, str], CalibrationLabel]
    other_by_key: dict[tuple[str, str, str], CalibrationLabel]
    shared_keys: tuple[tuple[str, str, str], ...]


def _verify_dual_annotator_coverage(
    labels: Sequence[CalibrationLabel], *, owner_annotator: str = OWNER_ANNOTATOR
) -> DualAnnotatorCoverage:
    """Verifies the dual-annotation design's core precondition (owner-
    approved 2026-07-09): exactly two annotators labeled ``round="initial"``,
    one of them ``owner_annotator``, and BOTH annotators labeled the exact
    same set of keys (complete coverage -- no partial-intersection tolerance,
    unlike the retired test-retest ceiling's ``>=2``-shared-keys allowance)
    with matching ``output_sha256`` per shared key. Feeds both
    ``compute_iaa_ceiling`` and ``resolve_gold_labels`` so the two can never
    disagree about what "complete, correctly-bound dual coverage" means.

    Raises ``DualAnnotationError`` (wrong annotator count/identity, or
    incomplete coverage -- message format: "second annotator labels
    incomplete: N keys missing") or ``CalibrationBindingError``
    (``output_sha256`` mismatch between the two annotators for a shared key).
    """

    by_annotator = _initial_labels_by_annotator(labels)
    annotators = sorted(by_annotator)
    if len(annotators) != 2:
        raise DualAnnotationError(
            "dual-annotation calibration requires exactly 2 annotators with round='initial' "
            f"labels; found {len(annotators)}: {annotators}"
        )
    if owner_annotator not in annotators:
        raise DualAnnotationError(
            f"dual-annotation calibration requires the owner annotator {owner_annotator!r} "
            f"among round='initial' labels; found only {annotators}"
        )
    other = next(a for a in annotators if a != owner_annotator)
    owner_by_key = by_annotator[owner_annotator]
    other_by_key = by_annotator[other]

    missing_from_other = sorted(set(owner_by_key) - set(other_by_key))
    missing_from_owner = sorted(set(other_by_key) - set(owner_by_key))
    if missing_from_other or missing_from_owner:
        n_missing = len(missing_from_other) + len(missing_from_owner)
        raise DualAnnotationError(
            f"second annotator labels incomplete: {n_missing} key(s) missing -- "
            f"{len(missing_from_other)} key(s) labeled by {owner_annotator!r} but not by "
            f"{other!r} ({missing_from_other}); {len(missing_from_owner)} key(s) labeled by "
            f"{other!r} but not by {owner_annotator!r} ({missing_from_owner})"
        )

    shared_keys = tuple(sorted(owner_by_key))
    mismatches = [
        key
        for key in shared_keys
        if owner_by_key[key].output_sha256 != other_by_key[key].output_sha256
    ]
    if mismatches:
        raise CalibrationBindingError(mismatches)

    return DualAnnotatorCoverage(
        owner=owner_annotator,
        other=other,
        owner_by_key=owner_by_key,
        other_by_key=other_by_key,
        shared_keys=shared_keys,
    )


def compute_iaa_ceiling(
    labels: Sequence[CalibrationLabel],
    *,
    keys: Sequence[tuple[str, str, str]] | None = None,
    ci_level: float = DEFAULT_CI_LEVEL,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    seed: int = 0,
) -> tuple[KappaResult, tuple[str, ...]]:
    """Cohen's kappa between the two annotators' verdicts over the doubly-
    labeled calibration set -- *the human-human agreement ceiling* that
    replaces the retired test-retest ceiling (owner-approved dual-annotation
    upgrade, 2026-07-09, constitution §6 D2): a judge kappa exceeding this
    value indicates estimation noise, not a super-human judge (unchanged
    semantics, new ceiling source). Coverage must be COMPLETE
    (``_verify_dual_annotator_coverage``) -- there is no partial-intersection
    tolerance the way the old ceiling had, because an incomplete second
    annotator undermines the ceiling's whole premise.

    ``keys`` (population-parity invariant, owner-ruled 2026-07-09), when
    given, restricts the computation to that key subset -- the SAME
    validly-judged population judge kappa is computed over (``_pair_
    entries``'s ``valid_keys``), so a judge error on one key shrinks the
    ceiling's population together with judge kappa's, never independently.
    Every key in ``keys`` is expected to already be one of the doubly-
    labeled ``shared_keys`` (guaranteed upstream by ``_pair_entries``'s
    population-parity check); a ``keys`` entry outside ``shared_keys`` is
    silently ignored rather than raised, since restriction is purely a
    subset filter. When omitted (the default), the ceiling is computed over
    the full doubly-labeled set -- used by direct/standalone callers of this
    function that have no judgment set to restrict against.
    """

    coverage = _verify_dual_annotator_coverage(labels)
    if keys is None:
        selected = coverage.shared_keys
    else:
        allowed = set(keys)
        selected = tuple(k for k in coverage.shared_keys if k in allowed)
    a = [coverage.owner_by_key[k].verdict for k in selected]
    b = [coverage.other_by_key[k].verdict for k in selected]
    clusters = [k[0] for k in selected]  # item_id (email)
    result, messages = _kappa_with_capture(
        a, b, clusters, ci_level=ci_level, n_resamples=n_resamples, seed=seed
    )
    return result, tuple(messages)


@dataclass(frozen=True)
class GoldLabel:
    """One resolved gold verdict (owner-approved dual-annotation upgrade,
    2026-07-09, ``resolve_gold_labels``): the owner's verdict when both
    annotators agree, or the OWNER's adjudication verdict when they disagree.
    ``source`` records which case produced it -- summed into ``Certificate.
    n_adjudicated`` (honest disclosure of how much of the gold set required a
    tie-break)."""

    item_id: str
    candidate: Literal["a", "b"]
    field: str
    verdict: Literal["pass", "fail"]
    critique: str
    output_sha256: str
    source: Literal["agreement", "adjudication"]


def _validate_adjudication_round(
    labels: Sequence[CalibrationLabel], coverage: DualAnnotatorCoverage
) -> dict[tuple[str, str, str], CalibrationLabel]:
    """Validates every ``round="adjudication"`` row up front, before any gold
    is resolved -- three schema-valid but semantically invalid shapes are
    collected together and reported in one loud ``DualAnnotationError``,
    never silently ignored or checked one at a time:

    1. A ``round="adjudication"`` row whose ``annotator`` is not
       ``coverage.owner`` -- spec §5 states adjudication rows are always the
       owner's; a stray non-owner row was previously filtered out invisibly
       by only ever looking up adjudications keyed to the owner.
    2. An owner adjudication row keyed to a (item_id, candidate, field)
       triple where the two annotators' ``"initial"`` verdicts already
       AGREE -- adjudication only makes sense for a disagreement. Leaving
       such a row in place and simply discarding its verdict (the previous
       behavior) means a later edit to either initial row that turns
       agreement into disagreement would silently pick up this stale row as
       gold, rather than surfacing that the row needs attention.
    3. An owner adjudication row keyed to a triple OUTSIDE the shared
       initial key set (a typo'd item_id/candidate/field) -- previously
       ignored entirely while the disagreement it was meant to resolve went
       on to fail as "unadjudicated", misdirecting the fix to the wrong
       place.

    Returns the validated ``{key: CalibrationLabel}`` map of owner
    adjudication rows -- every key in it is guaranteed to be one of
    ``coverage.shared_keys`` where the two annotators' initial verdicts
    disagree, ready for the hash check and gold resolution that follow.
    """

    non_owner_rows = sorted(
        (label.annotator, label.item_id, label.candidate, label.field)
        for label in labels
        if label.round == "adjudication" and label.annotator != coverage.owner
    )

    # Duplicate-key detection among the owner's own adjudication rows reuses
    # `_labels_by_annotator_round`'s existing all-or-nothing convention
    # (raises ValueError on a duplicate).
    adjudication_by_key = _labels_by_annotator_round(labels, coverage.owner, "adjudication")

    shared_key_set = set(coverage.shared_keys)
    agreed_keys = {
        key
        for key in coverage.shared_keys
        if coverage.owner_by_key[key].verdict == coverage.other_by_key[key].verdict
    }

    keys_outside_shared = sorted(key for key in adjudication_by_key if key not in shared_key_set)
    keys_on_agreed_verdicts = sorted(key for key in adjudication_by_key if key in agreed_keys)

    problems: list[str] = []
    if non_owner_rows:
        problems.append(
            f"{len(non_owner_rows)} adjudication row(s) from a non-owner annotator -- "
            "round='adjudication' rows must always be authored by the owner "
            f"({coverage.owner!r}, spec §5); (annotator, item_id, candidate, field): "
            f"{non_owner_rows}"
        )
    if keys_on_agreed_verdicts:
        problems.append(
            f"{len(keys_on_agreed_verdicts)} adjudication row(s) keyed to (item_id, candidate, "
            "field) triple(s) where the two annotators' initial verdicts already agree -- "
            f"adjudication only resolves a disagreement: {keys_on_agreed_verdicts}"
        )
    if keys_outside_shared:
        problems.append(
            f"{len(keys_outside_shared)} adjudication row(s) keyed to (item_id, candidate, "
            "field) triple(s) outside the shared initial-round key set (check for a typo'd "
            f"item_id/candidate/field): {keys_outside_shared}"
        )
    if problems:
        raise DualAnnotationError("; ".join(problems))

    return adjudication_by_key


def resolve_gold_labels(labels: Sequence[CalibrationLabel]) -> list[GoldLabel]:
    """Final gold labels for judge-agreement measurement (owner-approved
    dual-annotation upgrade, 2026-07-09): the owner's verdict where the two
    annotators' ``round="initial"`` labels agree, the OWNER's adjudication
    verdict (``round="adjudication"``, ``annotator="owner"``) where they
    disagree. Requires the same complete, correctly-bound dual coverage as
    ``compute_iaa_ceiling`` (``_verify_dual_annotator_coverage``) -- gold can
    never be resolved from an incomplete or unbound label set.

    The whole ``round="adjudication"`` round is validated up front
    (``_validate_adjudication_round``) before any gold is resolved: a
    non-owner adjudication row, an adjudication row on a key the two
    annotators already agree on, or an adjudication row keyed outside the
    shared initial key set each raise a loud ``DualAnnotationError`` naming
    every offending row.

    Every disagreement without a matching adjudication row is a loud
    ``DualAnnotationError`` naming every unadjudicated key -- gold is never
    partially resolved. An adjudication row whose ``output_sha256`` disagrees
    with the annotators' (the same binding check, extended to the
    adjudication round) raises ``CalibrationBindingError`` before any gold is
    resolved, all-or-nothing.
    """

    coverage = _verify_dual_annotator_coverage(labels)
    adjudication_by_key = _validate_adjudication_round(labels, coverage)

    adjudication_mismatches = sorted(
        key
        for key, adjudication in adjudication_by_key.items()
        if adjudication.output_sha256 != coverage.owner_by_key[key].output_sha256
    )
    if adjudication_mismatches:
        raise CalibrationBindingError(adjudication_mismatches)

    gold: list[GoldLabel] = []
    unadjudicated: list[tuple[str, str, str]] = []
    for key in coverage.shared_keys:
        owner_label = coverage.owner_by_key[key]
        other_label = coverage.other_by_key[key]
        if owner_label.verdict == other_label.verdict:
            gold.append(
                GoldLabel(
                    item_id=key[0],
                    candidate=key[1],
                    field=key[2],
                    verdict=owner_label.verdict,
                    critique=owner_label.critique,
                    output_sha256=owner_label.output_sha256,
                    source="agreement",
                )
            )
            continue
        adjudication = adjudication_by_key.get(key)
        if adjudication is None:
            unadjudicated.append(key)
            continue
        gold.append(
            GoldLabel(
                item_id=key[0],
                candidate=key[1],
                field=key[2],
                verdict=adjudication.verdict,
                critique=adjudication.critique,
                output_sha256=adjudication.output_sha256,
                source="adjudication",
            )
        )
    if unadjudicated:
        raise DualAnnotationError(
            f"{len(unadjudicated)} disagreement(s) between {coverage.owner!r} and "
            f"{coverage.other!r} have no adjudication row (round='adjudication', annotator="
            f"{coverage.owner!r}): {sorted(unadjudicated)}"
        )
    return gold


@dataclass(frozen=True)
class PairedJudgment:
    """One (gold label, judge verdict) pair -- both determinate -- ready to
    feed ``cohens_kappa``. ``owner_verdict`` is the resolved GOLD verdict
    (``resolve_gold_labels``): always owner-sourced, whether directly (the
    two annotators agreed) or via the owner's adjudication -- the field name
    predates the dual-annotation upgrade and remains accurate under it."""

    item_id: str
    candidate: Literal["a", "b"]
    owner_verdict: Literal["pass", "fail"]
    judge_verdict: Literal["pass", "fail"]


def _gold_by_key(gold: Sequence[GoldLabel]) -> dict[tuple[str, str, str], GoldLabel]:
    """``{(item_id, candidate, field): GoldLabel}``. Raises ``ValueError`` on
    a duplicate key -- ``resolve_gold_labels`` never produces one itself (one
    entry per shared key), so a duplicate here means a caller assembled
    ``gold`` by hand incorrectly."""

    by_key: dict[tuple[str, str, str], GoldLabel] = {}
    for g in gold:
        key = (g.item_id, g.candidate, g.field)
        if key in by_key:
            raise ValueError(f"duplicate gold label for {key}")
        by_key[key] = g
    return by_key


def _pair_entries(
    entries: Sequence[tuple[tuple[str, str, str], Literal["pass", "fail"] | None, str]],
    gold: Sequence[GoldLabel],
) -> tuple[list[PairedJudgment], int, tuple[tuple[str, str, str], ...]]:
    """Shared pairing/binding-check/population-parity core for
    ``pair_with_labels`` (live, entries carry a freshly-recomputed
    ``hash_output``) and ``pair_judgments_with_labels`` (offline, entries
    carry a previously-persisted hash) -- finding F1/F2, and the
    population-parity invariant (owner-ruled, 2026-07-09, module docstring).
    ``entries`` is ``(key, verdict, hash)`` per judged item, in judged order.

    Three checks run, each to completion over every entry, before any
    pairing happens -- all-or-nothing, in this order:

    1. **Output binding (finding F1):** any entry whose hash disagrees with
       its matching gold label's raises ``CalibrationBindingError`` naming
       every mismatched key.
    2. **Gold label with no corresponding judgment:** a key present in
       ``gold`` (the doubly-labeled, resolved set) but absent from
       ``entries`` raises ``DualAnnotationError`` -- nothing legitimate
       produces this; it means ``labels.jsonl`` and the judged/persisted
       judgment set have drifted apart.
    3. **Judgment that neither annotator labeled:** a key present in
       ``entries`` but absent from ``gold`` raises ``DualAnnotationError``
       -- this REPLACES the old ``unlabeled_excluded`` tolerance: a judged
       key with no label from either annotator is a labels-file gap, not a
       benign, silently-counted exclusion.

    After both population checks pass, ``gold`` and ``entries`` describe the
    EXACT same key set. The one tolerated gap is a judge error (``verdict is
    None``): those keys are excluded from the returned ``paired`` judgments
    and from ``valid_keys`` -- the SAME population the caller must also use
    for the ceiling kappa and ``n_adjudicated`` (population-parity
    invariant) -- while still counted in the returned ``judge_errors``
    count and disclosed in the report, never silently dropped.
    """

    by_gold = _gold_by_key(gold)

    mismatches = [
        key
        for key, _verdict, output_hash in entries
        if (g := by_gold.get(key)) is not None and g.output_sha256 != output_hash
    ]
    if mismatches:
        raise CalibrationBindingError(mismatches)

    judged_keys = {key for key, _verdict, _output_hash in entries}
    gold_keys = set(by_gold)

    gold_without_judgment = sorted(gold_keys - judged_keys)
    judged_without_gold = sorted(judged_keys - gold_keys)
    problems: list[str] = []
    if gold_without_judgment:
        problems.append(
            f"{len(gold_without_judgment)} gold label(s) have no corresponding judgment -- "
            "labels.jsonl and the judged/persisted judgment set describe different items; "
            "either the item was never judged or the judged/persisted data is stale "
            f"(item_id, candidate, field): {gold_without_judgment}"
        )
    if judged_without_gold:
        problems.append(
            f"{len(judged_without_gold)} judged key(s) were labeled by neither annotator -- "
            "every judged field must be doubly labeled (round='initial' by both annotators) "
            f"before it can be judged; add the missing labels (item_id, candidate, field): "
            f"{judged_without_gold}"
        )
    if problems:
        raise DualAnnotationError("; ".join(problems))

    paired: list[PairedJudgment] = []
    judge_errors = 0
    valid_keys: list[tuple[str, str, str]] = []
    for key, verdict, _output_hash in entries:
        g = by_gold[key]
        if verdict is None:
            judge_errors += 1
            continue
        valid_keys.append(key)
        paired.append(
            PairedJudgment(
                item_id=key[0], candidate=key[1], owner_verdict=g.verdict, judge_verdict=verdict
            )
        )
    return paired, judge_errors, tuple(valid_keys)


def pair_with_labels(
    judged: Sequence[JudgedTriple], gold: Sequence[GoldLabel]
) -> tuple[list[PairedJudgment], int, tuple[tuple[str, str, str], ...]]:
    """Joins judged triples to resolved GOLD labels (``resolve_gold_labels``).

    Returns ``(paired, judge_errors_excluded, valid_keys)``: a judge error
    (``verdict is None``) is excluded from ``paired``, never coerced to
    ``"fail"`` (spec §7), and its key is likewise excluded from
    ``valid_keys`` -- the population-parity invariant (owner-ruled,
    2026-07-09, module docstring): the caller must use this SAME
    ``valid_keys`` set to restrict the ceiling kappa and ``n_adjudicated``,
    so judge kappa and those two numbers are always computed over identical
    key sets. A judged triple with no matching gold label, or a gold label
    with no matching judged triple, is no longer silently excluded (the
    retired ``unlabeled_excluded`` tolerance) -- see ``_pair_entries``.

    **Output-binding check (finding F1):** before any of the above, every
    gold label whose key matches a judged triple has its ``output_sha256``
    checked against ``hash_output`` of that triple's LIVE ``candidate_value``
    -- see ``CalibrationBindingError``/module docstring for why, and why a
    mismatch raises rather than silently excludes.
    """

    entries = [
        (
            (jt.triple.item_id, jt.triple.candidate, jt.triple.field),
            jt.verdict,
            hash_output(jt.triple.candidate_value),
        )
        for jt in judged
    ]
    return _pair_entries(entries, gold)


def labeling_template_rows(triples: Sequence[Triple], annotator: str) -> list[dict]:
    """Prefilled labeling-material rows for one annotator's hash-bound
    labeling sheet (dual-annotation upgrade, 2026-07-09): one dict per triple
    with ``item_id``/``candidate``/``field``/``annotator``/``candidate_value``
    already filled in from ``triples``, plus a correctly computed
    ``output_sha256`` (``hash_output``) and empty ``verdict``/``critique``
    placeholders for that annotator to fill in by hand.

    Called once per annotator (same ``triples``, different ``annotator``) to
    produce each annotator's OWN sheet -- neither annotator's sheet carries
    the other's verdict column, since both are born blank; the sheets differ
    only in the ``annotator`` value stamped into every row.

    Exists so labeling artifacts are born correctly bound to the exact
    candidate output the annotator is looking at, rather than requiring a
    human (or some future generator) to hand-compute or copy-paste a hash
    that can silently drift from the value actually shown -- the row IS the
    hash's only input, computed here, once, from the same ``Triple`` the row
    displays.

    Each returned dict is shaped to become one ``CalibrationLabel`` once the
    annotator fills in ``verdict``/``critique`` (and ``label_id``/
    ``label_date``/``round``, which this function -- having no labeling
    session of its own -- does not know yet).
    """

    return [
        {
            "item_id": t.item_id,
            "candidate": t.candidate,
            "field": t.field,
            "annotator": annotator,
            "candidate_value": t.candidate_value,
            "output_sha256": hash_output(t.candidate_value),
            "verdict": "",
            "critique": "",
        }
        for t in triples
    ]


# --------------------------------------------------------------------------
# Perturbation overlay (fail-probe design, D2 amendment 2026-07-10): module
# docstring's "Fail-probe perturbation set" section.
# --------------------------------------------------------------------------


class PerturbationOverlayError(Exception):
    """Raised when ``data/calibration/perturbations.jsonl`` does not satisfy
    the fail-probe design's binding invariant (D2 amendment 2026-07-10):
    every overlay row's ``(item_id, candidate, field)`` key must reference an
    item from the fail-probe emails file AND an actually-judgeable triple
    reconstructed from that file's candidate runs -- never a key belonging to
    the original ``emails.jsonl``, never a key that doesn't exist at all
    (typo'd item_id/candidate/field), never a duplicate key within the
    overlay file itself. All violations are collected and named together in
    one message, all-or-nothing -- mirroring ``DualAnnotationError``/
    ``_validate_adjudication_round``'s existing convention -- never a partial
    application where some overlay rows apply and others are silently
    dropped."""


def validate_perturbation_overlay(
    overlay: Sequence[PerturbationOverlay],
    *,
    probe_item_ids: Collection[str],
    valid_probe_keys: Collection[tuple[str, str, str]],
) -> dict[tuple[str, str, str], PerturbationOverlay]:
    """Validates every row of ``overlay`` against the fail-probe run's own
    reconstructed triples, returning the validated ``{key: PerturbationOverlay}``
    mapping ``apply_perturbation_overlay`` consumes.

    ``probe_item_ids`` is every item id belonging to the fail-probe emails
    file (never the original ``emails.jsonl``); ``valid_probe_keys`` is every
    ``(item_id, candidate, field)`` key that actually exists among the
    fail-probe run's reconstructed ``Triple``s (live) or persisted judgments
    (offline, via ``JudgmentRecord.is_probe``) -- the SAME triples/judgments
    ``run_calibration``/``run_calibration_offline`` are about to judge/have
    already judged.

    Three violation kinds, checked per row and collected together (never
    raised on the first one found):

    1. A duplicate key within the overlay file itself.
    2. A key whose ``item_id`` is not among ``probe_item_ids`` -- it targets
       the original ``emails.jsonl`` (or some other, unrelated item) instead
       of the fail-probe file.
    3. A key whose ``item_id`` IS a probe item, but the full ``(item_id,
       candidate, field)`` key is not among ``valid_probe_keys`` -- nonexistent:
       a typo'd field/candidate, or a probe item the run never actually
       produced a judgeable output for.
    """

    probe_ids = set(probe_item_ids)
    valid_keys = set(valid_probe_keys)

    seen: set[tuple[str, str, str]] = set()
    duplicates: list[tuple[str, str, str]] = []
    targets_original_file: list[tuple[str, str, str]] = []
    nonexistent: list[tuple[str, str, str]] = []
    validated: dict[tuple[str, str, str], PerturbationOverlay] = {}

    for row in overlay:
        key = (row.item_id, row.candidate, row.field)
        if key in seen:
            duplicates.append(key)
            continue
        seen.add(key)
        if row.item_id not in probe_ids:
            targets_original_file.append(key)
            continue
        if key not in valid_keys:
            nonexistent.append(key)
            continue
        validated[key] = row

    problems: list[str] = []
    if targets_original_file:
        problems.append(
            f"{len(targets_original_file)} overlay row(s) target the original emails file "
            "(or some other item outside the fail-probe set), not "
            f"data/calibration/emails-fail-probe.jsonl: {sorted(targets_original_file)}"
        )
    if nonexistent:
        problems.append(
            f"{len(nonexistent)} overlay row(s) reference (item_id, candidate, field) key(s) "
            "that don't exist among the fail-probe run's judgeable triples (check for a "
            f"typo'd item_id/candidate/field): {sorted(nonexistent)}"
        )
    if duplicates:
        problems.append(
            f"{len(duplicates)} duplicate overlay row(s) for the same (item_id, candidate, "
            f"field) key in perturbations.jsonl: {sorted(duplicates)}"
        )
    if problems:
        raise PerturbationOverlayError("; ".join(problems))

    return validated


def apply_perturbation_overlay(
    triples: Sequence[Triple],
    overlay_by_key: Mapping[tuple[str, str, str], PerturbationOverlay],
) -> list[Triple]:
    """Replaces each fail-probe ``Triple``'s ``candidate_value`` with its
    overlay row's ``perturbed_value`` when a matching key exists in
    ``overlay_by_key`` (already validated by ``validate_perturbation_overlay``)
    -- everywhere downstream that reads the returned ``Triple``s (labeling
    sheets, label output-hash binding, ``judge_triples``) sees the overlaid
    text, never the real candidate output, for exactly these keys. A triple
    with no matching overlay entry is returned unchanged: a fail-probe item
    is not required to carry a planted fail."""

    result: list[Triple] = []
    for t in triples:
        overlay_row = overlay_by_key.get((t.item_id, t.candidate, t.field))
        if overlay_row is None:
            result.append(t)
        else:
            result.append(replace(t, candidate_value=overlay_row.perturbed_value))
    return result


def load_perturbation_overlay(path: str | Path) -> list[PerturbationOverlay]:
    """Parses ``data/calibration/perturbations.jsonl`` into
    ``PerturbationOverlay`` rows. Returns an empty list when ``path`` does
    not exist -- an absent overlay file is a valid, meaningful state (a
    fail-probe set with zero planted fails), mirroring the fail-probe emails
    file's own absent-file convention (spec §5: optional, absent = no
    overlay)."""

    path = Path(path)
    if not path.exists():
        return []
    rows: list[PerturbationOverlay] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(PerturbationOverlay.model_validate(json.loads(line)))
    return rows


# --------------------------------------------------------------------------
# Agreement statistics.
# --------------------------------------------------------------------------


def _kappa_with_capture(
    a: Sequence[str],
    b: Sequence[str],
    clusters: Sequence[str],
    *,
    ci_level: float,
    n_resamples: int,
    seed: int,
) -> tuple[KappaResult, list[str]]:
    """Calls ``cohens_kappa`` capturing any ``RuntimeWarning`` it emits
    (the degenerate-resample omission disclosure, D2/agreement.py) instead of
    letting it propagate to the caller's default warning handling -- the
    disclosure is rendered explicitly in the calibration report instead."""

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        result = cohens_kappa(
            a, b, clusters=clusters, level=ci_level, n_resamples=n_resamples, seed=seed
        )
    messages = [str(w.message) for w in caught if issubclass(w.category, RuntimeWarning)]
    return result, messages


def compute_agreement(
    paired: Sequence[PairedJudgment],
    *,
    ci_level: float = DEFAULT_CI_LEVEL,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    seed: int = 0,
) -> tuple[KappaResult, dict[str, KappaResult], tuple[str, ...]]:
    """Overall and per-candidate Cohen's kappa (with cluster-bootstrap CIs,
    clusters = email/``item_id``) over ``paired`` judgments -- spec §5, D2.

    Returns ``(overall, per_candidate, warnings)``: ``per_candidate`` is keyed
    by whichever candidate labels appear in ``paired`` (normally ``"a"``,
    ``"b"``); ``warnings`` pools every captured bootstrap-omission disclosure
    from the overall and per-candidate calls, in call order.
    """

    def _subset_kappa(subset: Sequence[PairedJudgment]) -> tuple[KappaResult, list[str]]:
        a = [p.owner_verdict for p in subset]
        b = [p.judge_verdict for p in subset]
        clusters = [p.item_id for p in subset]
        return _kappa_with_capture(
            a, b, clusters, ci_level=ci_level, n_resamples=n_resamples, seed=seed
        )

    overall, overall_warnings = _subset_kappa(paired)

    per_candidate: dict[str, KappaResult] = {}
    all_warnings = list(overall_warnings)
    for label in sorted({p.candidate for p in paired}):
        subset = [p for p in paired if p.candidate == label]
        result, msgs = _subset_kappa(subset)
        per_candidate[label] = result
        all_warnings.extend(msgs)

    return overall, per_candidate, tuple(all_warnings)


def compute_real_only_kappa(
    paired: Sequence[PairedJudgment],
    probe_item_ids: Collection[str],
    *,
    ci_level: float = DEFAULT_CI_LEVEL,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    seed: int = 0,
) -> tuple[KappaResult, tuple[str, ...]] | None:
    """Judge-vs-gold Cohen's kappa restricted to NON-probe items only
    (fail-probe design, D2 amendment 2026-07-10) -- reported ALONGSIDE the
    primary overall kappa (``compute_agreement``'s ``overall``), never
    replacing it as the adequacy-decision statistic. ``probe_item_ids`` is
    every item id belonging to the fail-probe set (both live and offline
    paths derive this independently -- see the module docstring); an empty
    ``probe_item_ids`` simply means every paired judgment is "real",
    reproducing the same population ``compute_agreement``'s overall kappa
    already used.

    Returns ``None`` when fewer than 2 non-probe paired judgments remain --
    ``cohens_kappa`` requires at least 2 paired observations, and callers
    only invoke this when a fail-probe set was actually used, so a
    real-only population this small signals nothing meaningful to compute.

    Also returns ``None`` when the non-probe subset collapses onto a single
    shared category -- gold verdict AND judge verdict both "pass" (or both
    "fail") for every real item (commit 471a41a review, Critical finding).
    This is not a corner case: it is the EXPECTED production state the
    fail-probe design exists to work around (module docstring -- the owner's
    real fail rate is 0% at prompt v4). Cohen's kappa is undefined (0/0) for
    a single-shared-category sample by ``harness.stats.agreement.
    cohens_kappa``'s documented convention (chance agreement hits 1), and
    ``harness.stats.bootstrap.bca_ci`` raises ``ValueError`` for a NaN
    *observed* statistic rather than silently returning a NaN CI -- that
    ValueError must never reach a caller of this function. Detecting the
    degenerate case up front (rather than calling ``cohens_kappa`` and
    catching the exception) mirrors the ``<2``-observations guard above:
    both are "there is nothing meaningful to compute" early returns, not
    error handling."""

    real_only = [p for p in paired if p.item_id not in probe_item_ids]
    if len(real_only) < 2:
        return None
    a = [p.owner_verdict for p in real_only]
    b = [p.judge_verdict for p in real_only]
    if len(set(a) | set(b)) < 2:
        return None
    clusters = [p.item_id for p in real_only]
    result, messages = _kappa_with_capture(
        a, b, clusters, ci_level=ci_level, n_resamples=n_resamples, seed=seed
    )
    return result, tuple(messages)


def decide_verdict(
    kappa: float, ci: tuple[float, float]
) -> Literal["adequate", "adequate_with_caveat", "inadequate"]:
    """Spec §5's adequacy policy, decided on the overall kappa POINT ESTIMATE:
    ``kappa >= 0.6`` with CI lower bound ``>= 0.4`` -> ``"adequate"``;
    ``kappa >= 0.6`` with CI lower bound ``< 0.4`` (gray zone) ->
    ``"adequate_with_caveat"``; otherwise ``"inadequate"``."""

    if kappa >= ADEQUACY_KAPPA_THRESHOLD:
        if ci[0] < GRAY_ZONE_CI_LOWER_THRESHOLD:
            return "adequate_with_caveat"
        return "adequate"
    return "inadequate"


def per_candidate_divergence_flag(per_candidate_kappa: Mapping[str, float]) -> bool:
    """``True`` iff the spread between per-candidate kappa point estimates
    exceeds 0.2 (spec §5/D1: a D1-review FLAG, never a gate condition). NaN
    kappas (degenerate single-category subsets) are excluded from the
    comparison -- fewer than two comparable values means no gap can be
    computed, so this returns ``False`` rather than raising."""

    values = [v for v in per_candidate_kappa.values() if not math.isnan(v)]
    if len(values) < 2:
        return False
    return (max(values) - min(values)) > DIVERGENCE_GAP_THRESHOLD


# --------------------------------------------------------------------------
# Self-consistency (spec §4).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SelfConsistencyResult:
    n_triples: int
    repeats: int
    flip_rate: float
    flipped_triples: tuple[tuple[str, str, str], ...]  # (item_id, candidate, field)


def select_fixed_self_consistency_triples(
    triples: Sequence[Triple], n: int = DEFAULT_SELF_CONSISTENCY_N
) -> list[Triple]:
    """A deterministic prefix of ``triples``, sorted by ``(item_id,
    candidate, field)`` -- the "20 fixed triples" spec §4 requires: the same
    calibration data always selects the same subset. Returns fewer than ``n``
    (never raises) if fewer triples are available -- only expected to matter
    for small test fixtures; real calibration data has 100 available."""

    ordered = sorted(triples, key=lambda t: (t.item_id, t.candidate, t.field))
    return ordered[:n]


@dataclass(frozen=True)
class SelfConsistencyRecord:
    """One persisted self-consistency repeat (finding F2): the
    ``judgment_index``-th (0-based) of ``repeats`` judge calls made against
    one fixed self-consistency triple, keyed by ``(item_id, candidate,
    field)`` like every other judged-triple key in this module."""

    item_id: str
    candidate: Literal["a", "b"]
    field: str
    judgment_index: int
    verdict: Literal["pass", "fail"] | None
    judge_version: str


def _measure_self_consistency_with_records(
    judge: Judge, triples: Sequence[Triple], *, repeats: int
) -> tuple[SelfConsistencyResult, list[SelfConsistencyRecord]]:
    """Does the actual judging for ``measure_self_consistency`` (below) and
    ``run_calibration``, additionally returning the raw per-repeat
    ``SelfConsistencyRecord``s so a live run can persist them (finding F2,
    ``write_judgments_jsonl``) without a second, duplicate round of judge
    calls -- ``measure_self_consistency`` is a thin wrapper that discards the
    records for callers that only want the aggregate stats."""

    jv = compute_judge_version()
    flipped: list[tuple[str, str, str]] = []
    records: list[SelfConsistencyRecord] = []
    for t in triples:
        verdicts: list[Literal["pass", "fail"] | None] = []
        for idx in range(repeats):
            verdict = judge.judge_field(t.email, t.field, t.reference, t.candidate_value).verdict
            verdicts.append(verdict)
            records.append(
                SelfConsistencyRecord(
                    item_id=t.item_id,
                    candidate=t.candidate,
                    field=t.field,
                    judgment_index=idx,
                    verdict=verdict,
                    judge_version=jv,
                )
            )
        determinate = [v for v in verdicts if v is not None]
        if len(determinate) >= 2 and len(set(determinate)) > 1:
            flipped.append((t.item_id, t.candidate, t.field))

    n = len(triples)
    flip_rate = len(flipped) / n if n else 0.0
    result = SelfConsistencyResult(
        n_triples=n, repeats=repeats, flip_rate=flip_rate, flipped_triples=tuple(flipped)
    )
    return result, records


def measure_self_consistency(
    judge: Judge, triples: Sequence[Triple], *, repeats: int = DEFAULT_SELF_CONSISTENCY_REPEATS
) -> SelfConsistencyResult:
    """Judges each of ``triples`` ``repeats`` times (spec §4: 3x), reporting
    the flip rate -- the fraction of triples whose repeated verdicts are not
    unanimous. A triple with fewer than 2 determinate (non-error) verdicts
    among its repeats cannot show a flip and is never counted as one."""

    result, _records = _measure_self_consistency_with_records(judge, triples, repeats=repeats)
    return result


def _self_consistency_from_records(
    records: Sequence[SelfConsistencyRecord],
) -> SelfConsistencyResult:
    """Reconstructs the aggregate ``SelfConsistencyResult`` from persisted
    ``SelfConsistencyRecord``s (finding F2, ``run_calibration_offline``) --
    the same flip-rate logic ``_measure_self_consistency_with_records`` uses,
    operating on already-judged data instead of making new judge calls."""

    by_triple: dict[tuple[str, str, str], list[SelfConsistencyRecord]] = {}
    for r in records:
        by_triple.setdefault((r.item_id, r.candidate, r.field), []).append(r)

    flipped: list[tuple[str, str, str]] = []
    repeats_seen: set[int] = set()
    for key, recs in by_triple.items():
        repeats_seen.add(len(recs))
        determinate = [r.verdict for r in recs if r.verdict is not None]
        if len(determinate) >= 2 and len(set(determinate)) > 1:
            flipped.append(key)

    n = len(by_triple)
    repeats = max(repeats_seen) if repeats_seen else DEFAULT_SELF_CONSISTENCY_REPEATS
    flip_rate = len(flipped) / n if n else 0.0
    return SelfConsistencyResult(
        n_triples=n, repeats=repeats, flip_rate=flip_rate, flipped_triples=tuple(sorted(flipped))
    )


# --------------------------------------------------------------------------
# Disjointness from the golden set (spec §5 AC).
# --------------------------------------------------------------------------


def check_disjoint_from_golden(
    calibration_items: Sequence[GoldenItem], golden_items: Sequence[GoldenItem]
) -> None:
    """Raises ``ValueError`` if any calibration item shares an id, or exact
    (subject, body) email content, with a golden item (spec §5: the
    calibration set must be disjoint from golden). Names every offending id
    so a violation is immediately actionable."""

    golden_ids = {item.id for item in golden_items}
    golden_emails = {(item.email.subject, item.email.body) for item in golden_items}

    id_overlap = sorted({item.id for item in calibration_items} & golden_ids)
    email_overlap = sorted(
        item.id
        for item in calibration_items
        if (item.email.subject, item.email.body) in golden_emails
    )
    if id_overlap or email_overlap:
        raise ValueError(
            "calibration items overlap with the golden set (spec §5, must be disjoint): "
            f"id overlap={id_overlap}, email-content overlap (calibration ids)={email_overlap}"
        )


# --------------------------------------------------------------------------
# Orchestration: run_calibration, build_certificate, render_calibration_report.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationResult:
    """Everything ``build_certificate``/``render_calibration_report`` need,
    already computed by ``run_calibration``/``run_calibration_offline``.

    ``judged_triples``/``self_consistency_records`` (additive, finding F2):
    only populated by ``run_calibration`` (the live path) -- the raw judge
    output the CLI persists to ``judgments.jsonl`` via ``write_judgments_
    jsonl``/``judgment_records_from_judged`` so a later ``--offline`` run can
    recompute without spending any API calls. ``run_calibration_offline``
    leaves them at their default ``()``: it already consumed a PERSISTED
    copy of this same data and does not reproduce it a second time.

    ``n_adjudicated`` (dual-annotation upgrade, 2026-07-09): count of gold
    labels resolved via owner adjudication rather than spontaneous agreement
    between the two annotators, restricted to the same valid, judge-error-
    free population as judge kappa (population-parity invariant, owner-ruled
    2026-07-09) -- honest disclosure, surfaced verbatim on ``Certificate.
    n_adjudicated``.

    ``unlabeled_excluded`` is retired (population-parity invariant,
    2026-07-09): a judged key with no label from either annotator is now a
    loud ``DualAnnotationError`` (``_pair_entries``), never a silently
    counted exclusion, so there is nothing left to disclose here.

    ``n_perturbed``/``achieved_fail_prevalence``/``real_only_kappa``/
    ``perturbed_rows_passed_by_gold``/``probe_item_ids`` (additive, D2
    amendment 2026-07-10 -- fail-probe/perturbation design, module
    docstring): all at their default (``None``/``frozenset()``) when no
    fail-probe set was used, reproducing pre-amendment behavior exactly.
    ``probe_item_ids`` is every item id the fail-probe run judged (used by
    the CLI to mark persisted judgments' ``JudgmentRecord.is_probe`` for
    offline parity) -- disclosed on ``CalibrationResult`` but NOT itself
    written to ``Certificate`` (only the derived counts/statistics are).
    """

    judge_version: str
    label_file_hash: str
    date: date
    overall: KappaResult
    per_candidate: dict[str, KappaResult]
    verdict: Literal["adequate", "adequate_with_caveat", "inadequate"]
    divergence_flag: bool
    initial_fail_rate: float
    fail_enrichment_note: bool
    judge_errors_excluded: int
    self_consistency: SelfConsistencyResult
    ceiling: KappaResult | None
    warnings: tuple[str, ...]
    judged_triples: tuple[JudgedTriple, ...] = ()
    self_consistency_records: tuple[SelfConsistencyRecord, ...] = ()
    n_adjudicated: int = 0
    n_perturbed: int | None = None
    achieved_fail_prevalence: float | None = None
    real_only_kappa: KappaResult | None = None
    perturbed_rows_passed_by_gold: int | None = None
    probe_item_ids: frozenset[str] = frozenset()


def run_calibration(
    *,
    run_a: RunArtifact,
    run_b: RunArtifact,
    labels: Sequence[CalibrationLabel],
    judge: Judge,
    label_file_hash: str,
    probe_run_a: RunArtifact | None = None,
    probe_run_b: RunArtifact | None = None,
    perturbation_overlay: Sequence[PerturbationOverlay] = (),
    date_override: date | None = None,
    self_consistency_n: int = DEFAULT_SELF_CONSISTENCY_N,
    self_consistency_repeats: int = DEFAULT_SELF_CONSISTENCY_REPEATS,
    ci_level: float = DEFAULT_CI_LEVEL,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    seed: int = 0,
) -> CalibrationResult:
    """The full judge-calibration measurement (spec §5, §4, dual-annotation
    upgrade 2026-07-09; fail-probe perturbation set, D2 amendment 2026-07-10):
    reconstructs triples from both candidates' persisted calibration runs,
    re-judges them with the current judge, resolves the FINAL gold labels
    from the two annotators' labels (``resolve_gold_labels`` -- owner
    adjudication wins every disagreement), computes overall/per-candidate
    judge agreement against that gold, the adequacy verdict, the
    per-candidate divergence flag, the stratification fail-rate note, judge
    self-consistency, and the human-human agreement (IAA) ceiling
    (``compute_iaa_ceiling``) -- both gold resolution and the ceiling are
    computed unconditionally now (no opt-in flag): the retired
    single-annotator design's degraded, ceiling-less mode no longer exists.

    ``probe_run_a``/``probe_run_b`` (fail-probe design, module docstring):
    when either is given, that candidate's fail-probe run (``data/
    calibration/emails-fail-probe.jsonl``, same ``build_triples`` seam) is
    reconstructed, its triples validated against ``perturbation_overlay``
    (``validate_perturbation_overlay``) and overlaid (``apply_perturbation_
    overlay``), then concatenated with the real triples -- every downstream
    statistic (gold resolution, judge agreement, the ceiling, population
    parity) runs over that UNION unchanged, per spec §5's superseding
    fail-probe rule. Omitting both (the default) reproduces pre-amendment
    behavior exactly, including every disclosure field staying ``None``.

    Raises ``DualAnnotationError``/``CalibrationBindingError`` (via
    ``resolve_gold_labels``/``pair_with_labels``/``compute_iaa_ceiling``) if
    the two annotators' labels do not satisfy the dual-annotation
    precondition, ``PerturbationOverlayError`` if ``perturbation_overlay``
    does not satisfy its own binding invariant, or ``ValueError`` if no
    judged triple has a matching gold label at all -- there is nothing to
    compute agreement on, which signals a wiring problem (mismatched
    dataset/label files) rather than a real calibration outcome to report on.

    Gold is resolved BEFORE the judge is ever invoked: every
    ``DualAnnotationError``/``CalibrationBindingError`` ``resolve_gold_
    labels`` can raise is computable from ``labels`` alone, so a labels-file
    defect fails before spending a single judge call on ``judge_triples``
    (finding: a ~100-call judge spend used to run first and was never
    persisted on failure, re-burning the full spend on every labels-file fix
    iteration). The overlay is validated and applied right after that, still
    before any judge call, for the same reason -- a broken perturbations.jsonl
    must never burn judge spend either. ``pair_with_labels``'s
    population-parity checks (a gold label with no corresponding judgment,
    or a judged key labeled by neither annotator -- owner-ruled 2026-07-09,
    module docstring) necessarily run AFTER judging instead, since they
    depend on the judgment set; their result, ``valid_keys``, then restricts
    the ceiling kappa and ``n_adjudicated`` to the SAME population judge
    kappa was computed over.
    """

    triples_a = build_triples("a", run_a)
    triples_b = build_triples("b", run_b)
    main_triples = triples_a + triples_b

    probe_triples: list[Triple] = []
    if probe_run_a is not None:
        probe_triples += build_triples("a", probe_run_a)
    if probe_run_b is not None:
        probe_triples += build_triples("b", probe_run_b)

    has_probe_set = probe_run_a is not None or probe_run_b is not None
    probe_item_ids: set[str] = set()
    if probe_run_a is not None:
        probe_item_ids |= {item.id for item in probe_run_a.items}
    if probe_run_b is not None:
        probe_item_ids |= {item.id for item in probe_run_b.items}

    overlay_by_key = validate_perturbation_overlay(
        perturbation_overlay,
        probe_item_ids=probe_item_ids,
        valid_probe_keys={(t.item_id, t.candidate, t.field) for t in probe_triples},
    )
    probe_triples = apply_perturbation_overlay(probe_triples, overlay_by_key)

    all_triples = main_triples + probe_triples

    gold = resolve_gold_labels(labels)

    judged = judge_triples(judge, all_triples)

    # Population-parity invariant (owner-ruled, 2026-07-09, module
    # docstring): these checks depend on the judgment set, so -- unlike the
    # labels-only validations inside resolve_gold_labels above -- they
    # necessarily run after judging. `valid_keys` is the SAME population the
    # ceiling kappa and n_adjudicated below must also use. It spans the
    # UNION of real + probe triples (fail-probe design) unchanged.
    paired, judge_errors_excluded, valid_keys = pair_with_labels(judged, gold)
    if not paired:
        raise ValueError(
            "no gold label matched a judged calibration output -- check that "
            "labels.jsonl and the calibration runs describe the same items"
        )

    overall, per_candidate, agreement_warnings = compute_agreement(
        paired, ci_level=ci_level, n_resamples=n_resamples, seed=seed
    )
    verdict = decide_verdict(overall.kappa, overall.ci)
    divergence = per_candidate_divergence_flag(
        {label: result.kappa for label, result in per_candidate.items()}
    )

    # Stratification fail-rate is measured on the OWNER's own initial labels
    # specifically (not both annotators' combined rows, which would double
    # count) -- the owner is the primary annotator who drives the
    # stratification loop (spec §5), unchanged by the dual-annotation upgrade.
    owner_initial_labels = [
        label for label in labels if label.round == "initial" and label.annotator == OWNER_ANNOTATOR
    ]
    fail_count = sum(1 for label in owner_initial_labels if label.verdict == "fail")
    initial_fail_rate = fail_count / len(owner_initial_labels) if owner_initial_labels else 0.0
    fail_enrichment_note = initial_fail_rate < STRATIFICATION_FAIL_RATE_THRESHOLD

    fixed_triples = select_fixed_self_consistency_triples(all_triples, self_consistency_n)
    self_consistency, self_consistency_records = _measure_self_consistency_with_records(
        judge, fixed_triples, repeats=self_consistency_repeats
    )

    # Population-parity invariant: the ceiling is restricted to `valid_keys`
    # -- the exact same key set judge kappa was just computed over -- and
    # n_adjudicated is restricted to that same set, so a judge error on one
    # key shrinks both together (never just judge kappa, as before).
    ceiling, ceiling_warnings = compute_iaa_ceiling(
        labels, keys=valid_keys, ci_level=ci_level, n_resamples=n_resamples, seed=seed
    )
    valid_key_set = set(valid_keys)
    n_adjudicated = sum(
        1
        for g in gold
        if g.source == "adjudication" and (g.item_id, g.candidate, g.field) in valid_key_set
    )

    resolved_date = resolve_certificate_date(labels, date_override)

    # Fail-probe disclosure (D2 amendment 2026-07-10): only populated when a
    # probe set was actually used -- omitting both probe_run_a/probe_run_b
    # (the default) reproduces every one of these fields as None exactly.
    n_perturbed: int | None = None
    achieved_fail_prevalence: float | None = None
    real_only_kappa: KappaResult | None = None
    perturbed_rows_passed_by_gold: int | None = None
    real_only_warnings: tuple[str, ...] = ()
    if has_probe_set:
        n_perturbed = len(overlay_by_key)
        achieved_fail_prevalence = 1.0 - overall.prevalence
        real_only = compute_real_only_kappa(
            paired, probe_item_ids, ci_level=ci_level, n_resamples=n_resamples, seed=seed
        )
        if real_only is not None:
            real_only_kappa, real_only_warnings = real_only
        perturbed_rows_passed_by_gold = sum(
            1
            for g in gold
            if (g.item_id, g.candidate, g.field) in overlay_by_key and g.verdict == "pass"
        )

    return CalibrationResult(
        judge_version=compute_judge_version(),
        label_file_hash=label_file_hash,
        date=resolved_date,
        overall=overall,
        per_candidate=per_candidate,
        verdict=verdict,
        divergence_flag=divergence,
        initial_fail_rate=initial_fail_rate,
        fail_enrichment_note=fail_enrichment_note,
        judge_errors_excluded=judge_errors_excluded,
        self_consistency=self_consistency,
        ceiling=ceiling,
        warnings=agreement_warnings + ceiling_warnings + real_only_warnings,
        judged_triples=tuple(judged),
        self_consistency_records=tuple(self_consistency_records),
        n_adjudicated=n_adjudicated,
        n_perturbed=n_perturbed,
        achieved_fail_prevalence=achieved_fail_prevalence,
        real_only_kappa=real_only_kappa,
        perturbed_rows_passed_by_gold=perturbed_rows_passed_by_gold,
        probe_item_ids=frozenset(probe_item_ids),
    )


def build_certificate(result: CalibrationResult) -> Certificate:
    """Builds the committed ``Certificate`` (spec §5) from an already-computed
    ``CalibrationResult`` -- every field named in spec §5, including the
    additive ``per_candidate_kappa_ci`` (T14), from the dual-annotation
    upgrade (2026-07-09) ``ceiling_kappa_ci`` and ``n_adjudicated``, and from
    the fail-probe/perturbation design (D2 amendment 2026-07-10)
    ``n_perturbed``/``achieved_fail_prevalence``/``real_only_kappa``/
    ``real_only_kappa_ci``/``perturbed_rows_passed_by_gold`` -- all ``None``
    when ``result`` carries no fail-probe disclosure (no probe set used)."""

    return Certificate(
        judge_version=result.judge_version,
        overall_kappa=result.overall.kappa,
        kappa_ci=result.overall.ci,
        per_candidate_kappa={label: r.kappa for label, r in result.per_candidate.items()},
        per_candidate_kappa_ci={label: r.ci for label, r in result.per_candidate.items()},
        verdict=result.verdict,
        ceiling_kappa=result.ceiling.kappa if result.ceiling is not None else None,
        ceiling_kappa_ci=result.ceiling.ci if result.ceiling is not None else None,
        n_adjudicated=result.n_adjudicated,
        label_file_hash=result.label_file_hash,
        date=result.date,
        n_perturbed=result.n_perturbed,
        achieved_fail_prevalence=result.achieved_fail_prevalence,
        real_only_kappa=(
            result.real_only_kappa.kappa if result.real_only_kappa is not None else None
        ),
        real_only_kappa_ci=(
            result.real_only_kappa.ci if result.real_only_kappa is not None else None
        ),
        perturbed_rows_passed_by_gold=result.perturbed_rows_passed_by_gold,
    )


def render_calibration_report(result: CalibrationResult) -> str:
    """Renders the ``eval calibrate`` markdown report: agreement (overall +
    per-candidate, with CIs, raw agreement, prevalence as descriptive
    context), the adequacy verdict, the D1-review divergence flag when it
    fires, the stratification fail-rate note, judge self-consistency, the
    human-human agreement (IAA) ceiling row (dual-annotation upgrade,
    2026-07-09, when computed) with the adjudicated-disagreement count, and
    any bootstrap-omission disclosures.

    When a fail-probe set was used but ``result.real_only_kappa`` is
    ``None`` (``compute_real_only_kappa``'s degenerate-real-subset case,
    commit 471a41a review), the Perturbation Probe Set section renders an
    explicit "undefined" disclosure line instead of silently omitting the
    real-only κ bullet -- never hidden, mirroring this module's other
    disclosure conventions."""

    lines: list[str] = ["# Judge Calibration Report", ""]
    lines.append(f"- Judge version: `{result.judge_version}`")
    lines.append(f"- Certificate date: {result.date.isoformat()}")
    lines.append(f"- Label file hash: `{result.label_file_hash}`")
    lines.append("")

    lines.append("## Agreement")
    lines.append("")
    lines.append(
        f"- Overall Cohen's κ = {result.overall.kappa:.3f} (95% cluster-bootstrap CI "
        f"[{result.overall.ci[0]:.3f}, {result.overall.ci[1]:.3f}])"
    )
    lines.append(
        f"- Raw agreement (descriptive context only): {result.overall.raw_agreement:.1%}"
    )
    lines.append(
        f"- Label prevalence, pass (descriptive context only): {result.overall.prevalence:.1%}"
    )
    lines.append(f"- Verdict: **{result.verdict}**")
    if result.verdict == "adequate_with_caveat":
        lines.append(
            "  - Gray zone: κ̂ >= 0.6 but the CI lower bound < 0.4 (spec §5) -- flagged, not gated."
        )
    lines.append("")

    lines.append("### Per-Candidate")
    lines.append("")
    for label in sorted(result.per_candidate):
        r = result.per_candidate[label]
        lines.append(
            f"- candidate {label}: κ = {r.kappa:.3f} (95% CI [{r.ci[0]:.3f}, {r.ci[1]:.3f}]), "
            f"raw agreement {r.raw_agreement:.1%}, prevalence {r.prevalence:.1%}"
        )
    lines.append("")
    if result.divergence_flag:
        lines.append(
            "> **D1-review flag:** per-candidate κ differs by more than 0.2 -- differential "
            "judge error across candidates (spec §5/D1). A flag for review, never a gate "
            "condition."
        )
        lines.append("")

    lines.append(
        f"Excluded from judge kappa, the ceiling kappa, and n_adjudicated alike "
        f"(population-parity invariant): {result.judge_errors_excluded} judge error(s) (never "
        "counted as fail, spec §7)."
    )
    lines.append("")

    lines.append("## Stratification")
    lines.append("")
    lines.append(f"Initial fail-label rate: {result.initial_fail_rate:.1%}.")
    if result.fail_enrichment_note:
        lines.append("")
        lines.append(
            "> Below the 20% stratification-loop threshold (spec §5): the calibration set "
            "was enriched with harder-category emails after the initial round. Agreement "
            "above is measured on a harder-than-operational distribution -- a conservative "
            "estimate, not an inflated one."
        )
    lines.append("")

    lines.append("## Judge Self-Consistency")
    lines.append("")
    lines.append(
        f"{result.self_consistency.n_triples} fixed (email, reference, candidate-value) "
        f"triple(s), each judged {result.self_consistency.repeats}x: flip rate = "
        f"{result.self_consistency.flip_rate:.1%} "
        f"({len(result.self_consistency.flipped_triples)}/{result.self_consistency.n_triples})."
    )
    lines.append("")

    if result.ceiling is not None:
        lines.append("## Human-Human Agreement Ceiling")
        lines.append("")
        lines.append(
            f"Inter-annotator κ between the two independent annotators' verdicts = "
            f"{result.ceiling.kappa:.3f} (95% cluster-bootstrap CI "
            f"[{result.ceiling.ci[0]:.3f}, {result.ceiling.ci[1]:.3f}]) -- the human-human "
            "agreement ceiling. A judge κ exceeding this value indicates estimation noise, "
            "not a super-human judge. (The ceiling measures agreement under the written "
            "conventions with owner-adjudicated gold -- not a fully independent third "
            "check.)"
        )
        lines.append("")
        lines.append(f"Adjudicated disagreements: {result.n_adjudicated}.")
        lines.append("")

    if result.n_perturbed is not None:
        lines.append("## Perturbation Probe Set")
        lines.append("")
        lines.append(
            "Fail-side content is controlled perturbation of real candidate outputs "
            "(D2 amendment 2026-07-10), disclosed here rather than distribution-shifted "
            "email selection -- superseding spec §5's fail-enrichment paragraph for this path."
        )
        lines.append(f"- Overlaid rows (n_perturbed): {result.n_perturbed}")
        if result.achieved_fail_prevalence is not None:
            lines.append(
                "- Achieved fail prevalence of the resolved gold (combined, probe-included "
                f"population): {result.achieved_fail_prevalence:.1%}"
            )
        if result.real_only_kappa is not None:
            rok = result.real_only_kappa
            if rok.prevalence in (0.0, 1.0):
                # Single-category GOLD marginal: kappa is algebraically 0 no
                # matter what the judge does (p_o == p_e identically), and
                # every bootstrap replicate is 0 -- a structural value, not a
                # measurement. Rendering it with the informative-kappa framing
                # invites the misreading "zero chance-corrected agreement on
                # real data" (final whole-branch review 2026-07-20, I2).
                miss_kind = "false-fail" if rok.prevalence == 1.0 else "false-pass"
                lines.append(
                    f"- Real-only κ (judge vs. gold, non-probe items only) = "
                    f"{rok.kappa:.3f} (95% cluster-bootstrap CI "
                    f"[{rok.ci[0]:.3f}, {rok.ci[1]:.3f}]) -- **structurally zero**: the "
                    f"resolved gold is single-category on the real subset (pass-prevalence "
                    f"{rok.prevalence:.1%}), and with a constant gold marginal Cohen's κ is "
                    "algebraically 0 regardless of judge behavior. The meaningful "
                    f"real-subset number is raw agreement: {rok.raw_agreement:.1%} (every "
                    f"disagreement is a judge {miss_kind} relative to gold). Reported "
                    "alongside the primary overall κ above, never replacing it as the "
                    "decision statistic."
                )
            else:
                lines.append(
                    f"- Real-only κ (judge vs. gold, non-probe items only) = "
                    f"{rok.kappa:.3f} (95% cluster-bootstrap CI "
                    f"[{rok.ci[0]:.3f}, {rok.ci[1]:.3f}]) -- "
                    "reported alongside the primary overall κ above, never replacing it as the "
                    "decision statistic."
                )
        else:
            lines.append(
                "- Real-only κ (judge vs. gold, non-probe items only): **undefined** -- the "
                "real subset is single-category (judge and gold agree \"pass\" everywhere), so "
                "chance agreement is 1 and kappa is 0/0 (harness.stats.agreement's documented "
                "convention). This is the expected production state the fail-probe set exists "
                "to work around (the real items essentially never fail); disclosed here rather "
                "than silently omitted."
            )
        lines.append(
            f"- Perturbed rows the resolved gold still passed: "
            f"{result.perturbed_rows_passed_by_gold}"
        )
        lines.append("")

    if result.warnings:
        lines.append("## Bootstrap Disclosures")
        lines.append("")
        for message in result.warnings:
            lines.append(f"- {message}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------
# Persisted judge output / zero-API offline recompute (finding F2):
# data/calibration/judgments.jsonl.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgmentRecord:
    """One persisted main-agreement judgment (finding F2) -- a judged
    triple's verdict plus enough (``output_sha256``, ``judge_version``) to
    re-verify its label binding and judge currency later without needing the
    ``RunArtifact`` this triple was originally reconstructed from.

    ``is_probe`` (additive, D2 amendment 2026-07-10 -- fail-probe design):
    ``True`` iff this judgment's triple came from the fail-probe run
    (``data/calibration/emails-fail-probe.jsonl``), never the original
    calibration emails. Defaults ``False`` so every pre-existing
    ``judgments.jsonl`` round-trips unchanged. This is the ONLY signal
    ``run_calibration_offline`` needs to derive its own probe-item and
    valid-probe-key sets -- no ``RunArtifact``/candidate items required
    offline at all (module docstring)."""

    item_id: str
    candidate: Literal["a", "b"]
    field: str
    verdict: Literal["pass", "fail"] | None
    error: str | None
    rationale: str | None
    output_sha256: str
    judge_version: str
    is_probe: bool = False


@dataclass(frozen=True)
class JudgmentsFile:
    """Parsed ``data/calibration/judgments.jsonl`` (finding F2): a header
    (``judge_version``, ``written_at``) plus every persisted main-agreement
    judgment and self-consistency repeat from one live ``eval calibrate``
    run. ``run_calibration_offline`` consumes this -- and nothing else -- to
    recompute the full report/certificate with zero client construction."""

    judge_version: str
    written_at: str
    judgments: tuple[JudgmentRecord, ...]
    self_consistency: tuple[SelfConsistencyRecord, ...]


def judgment_records_from_judged(
    judged: Sequence[JudgedTriple],
    *,
    judge_version: str,
    probe_item_ids: Collection[str] = (),
) -> list[JudgmentRecord]:
    """Converts a live run's freshly-judged triples into the persisted
    ``JudgmentRecord`` shape ``write_judgments_jsonl`` writes (finding F2) --
    each carries ``hash_output`` of its triple's ``candidate_value`` so a
    later ``--offline`` recompute can re-verify the same output-binding check
    ``pair_with_labels`` already enforces live, without needing the
    candidate's raw output a second time.

    ``probe_item_ids`` (fail-probe design, D2 amendment 2026-07-10; default
    ``()`` reproduces pre-amendment behavior exactly, every record
    ``is_probe=False``): every item id belonging to the fail-probe run
    (``CalibrationResult.probe_item_ids``) -- stamped onto each record's
    ``is_probe`` so ``run_calibration_offline`` can recover probe-item
    membership from persisted data alone."""

    probe_ids = set(probe_item_ids)
    return [
        JudgmentRecord(
            item_id=jt.triple.item_id,
            candidate=jt.triple.candidate,
            field=jt.triple.field,
            verdict=jt.verdict,
            error=jt.error,
            rationale=jt.rationale,
            output_sha256=hash_output(jt.triple.candidate_value),
            judge_version=judge_version,
            is_probe=jt.triple.item_id in probe_ids,
        )
        for jt in judged
    ]


def pair_judgments_with_labels(
    judgments: Sequence[JudgmentRecord], gold: Sequence[GoldLabel]
) -> tuple[list[PairedJudgment], int, tuple[tuple[str, str, str], ...]]:
    """The offline counterpart to ``pair_with_labels`` (finding F2): joins
    PERSISTED judgments (not freshly re-judged triples) to resolved GOLD
    labels, with the same all-or-nothing output-binding check -- except the
    check compares each judgment's PERSISTED ``output_sha256`` against the
    gold label's directly (no candidate output is available offline to
    re-hash), rather than recomputing the hash from a live ``candidate_value``
    the way ``pair_with_labels`` does. See ``CalibrationBindingError``/module
    docstring.

    Returns ``(paired, judge_errors_excluded, valid_keys)`` -- the same
    population-parity contract ``pair_with_labels`` returns (see there and
    ``_pair_entries``): the caller must restrict the ceiling kappa and
    ``n_adjudicated`` to this SAME ``valid_keys`` set."""

    entries = [
        ((j.item_id, j.candidate, j.field), j.verdict, j.output_sha256) for j in judgments
    ]
    return _pair_entries(entries, gold)


class StaleJudgmentsError(Exception):
    """Raised by ``run_calibration_offline`` when persisted ``judgments.
    jsonl``'s ``judge_version`` does not match the CURRENT ``judge_version()``
    (finding F2): the judge prompt/rubric/few-shots/model has changed since
    those judgments were produced, so recomputing a certificate from them
    would silently certify a DIFFERENT judge than the one actually in use.
    Never auto-resolved by re-judging -- the message instructs the operator
    to re-run ``eval calibrate`` live instead, which re-judges with the
    current judge and refreshes ``judgments.jsonl``."""

    def __init__(self, recorded: str, current: str) -> None:
        super().__init__(
            f"data/calibration/judgments.jsonl was produced by judge_version={recorded!r}, "
            f"but the CURRENT judge is judge_version={current!r} -- stale judgments cannot "
            "certify the current judge. Re-run `eval calibrate` live (without --offline) to "
            "re-judge with the current judge and refresh judgments.jsonl."
        )
        self.recorded = recorded
        self.current = current


def run_calibration_offline(
    *,
    judgments: JudgmentsFile,
    labels: Sequence[CalibrationLabel],
    label_file_hash: str,
    perturbation_overlay: Sequence[PerturbationOverlay] = (),
    date_override: date | None = None,
    ci_level: float = DEFAULT_CI_LEVEL,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    seed: int = 0,
) -> CalibrationResult:
    """Zero-API-call recompute of the FULL judge-calibration report +
    certificate (finding F2, spec AC5), purely from a previously-persisted
    ``judgments.jsonl`` (a live ``eval calibrate`` run's own judge output) and
    ``labels.jsonl`` -- no ``Judge``/``ModelClient``/candidate run is ever
    touched or constructed. Mirrors ``run_calibration``'s pipeline exactly,
    substituting persisted judgments for a freshly re-judged triple set (gold
    resolution is unaffected either way: it only ever reads ``labels``, live
    or offline). Unlike the live path, every input here is already on disk,
    so every check -- gold resolution's labels-only validations, the
    perturbation overlay's own validation, AND ``pair_judgments_with_
    labels``'s population-parity checks alike (owner-ruled, 2026-07-09,
    module docstring) -- runs before any statistic is computed.

    ``perturbation_overlay`` (fail-probe design, D2 amendment 2026-07-10;
    default ``()`` reproduces pre-amendment behavior exactly): re-validated
    against probe-item/valid-probe-key sets derived PURELY from
    ``judgments.judgments``'s persisted ``is_probe`` flag -- no
    ``RunArtifact``/candidate items needed offline at all (module docstring).
    A probe set is considered "used" iff at least one persisted judgment has
    ``is_probe=True``; the fail-probe disclosure fields on the returned
    ``CalibrationResult`` are populated identically to ``run_calibration``'s
    in that case, ``None`` otherwise.

    Fails loudly rather than silently recompute against data the certificate
    can no longer trust:

    - ``StaleJudgmentsError`` if ``judgments.judge_version`` disagrees with
      the CURRENT ``judge_version()`` -- these judgments no longer describe
      the judge in use; re-run live.
    - ``DualAnnotationError``/``CalibrationBindingError`` if the two
      annotators' labels do not satisfy the dual-annotation precondition
      (``resolve_gold_labels``/``compute_iaa_ceiling``).
    - ``PerturbationOverlayError`` if ``perturbation_overlay`` does not
      satisfy its own binding invariant against the persisted judgments.
    - ``DualAnnotationError`` if a gold label has no corresponding persisted
      judgment, or a persisted judgment's key was labeled by neither
      annotator (population-parity invariant, ``pair_judgments_with_
      labels``/``_pair_entries``) -- replaces the retired ``unlabeled_
      excluded`` tolerance.
    - ``CalibrationBindingError`` if any judgment's persisted
      ``output_sha256`` disagrees with its matching gold label's (the same
      F1 binding check ``pair_with_labels`` runs live, applied here to the
      PERSISTED hash instead of a freshly recomputed one).
    - ``ValueError`` if no persisted judgment matches a gold label at all
      (mirrors ``run_calibration``'s own guard) -- a wiring problem
      (mismatched judgments/labels files), not a real calibration outcome.
    """

    current_judge_version = compute_judge_version()
    if judgments.judge_version != current_judge_version:
        raise StaleJudgmentsError(judgments.judge_version, current_judge_version)

    gold = resolve_gold_labels(labels)

    # Fail-probe design (D2 amendment 2026-07-10): probe-item/valid-probe-key
    # sets are derived purely from the persisted `is_probe` flag -- no
    # RunArtifact needed offline (module docstring).
    probe_item_ids = {j.item_id for j in judgments.judgments if j.is_probe}
    has_probe_set = bool(probe_item_ids)
    valid_probe_keys = {
        (j.item_id, j.candidate, j.field) for j in judgments.judgments if j.is_probe
    }
    overlay_by_key = validate_perturbation_overlay(
        perturbation_overlay, probe_item_ids=probe_item_ids, valid_probe_keys=valid_probe_keys
    )

    # Population-parity invariant (owner-ruled, 2026-07-09, module
    # docstring): `valid_keys` is the same population the ceiling kappa and
    # n_adjudicated below must also be restricted to. Spans the UNION of
    # real + probe judgments (fail-probe design) unchanged.
    paired, judge_errors_excluded, valid_keys = pair_judgments_with_labels(
        judgments.judgments, gold
    )
    if not paired:
        raise ValueError(
            "no gold label matched a persisted calibration judgment -- check that "
            "labels.jsonl and judgments.jsonl describe the same items"
        )

    overall, per_candidate, agreement_warnings = compute_agreement(
        paired, ci_level=ci_level, n_resamples=n_resamples, seed=seed
    )
    verdict = decide_verdict(overall.kappa, overall.ci)
    divergence = per_candidate_divergence_flag(
        {label: result.kappa for label, result in per_candidate.items()}
    )

    owner_initial_labels = [
        label for label in labels if label.round == "initial" and label.annotator == OWNER_ANNOTATOR
    ]
    fail_count = sum(1 for label in owner_initial_labels if label.verdict == "fail")
    initial_fail_rate = fail_count / len(owner_initial_labels) if owner_initial_labels else 0.0
    fail_enrichment_note = initial_fail_rate < STRATIFICATION_FAIL_RATE_THRESHOLD

    self_consistency = _self_consistency_from_records(judgments.self_consistency)

    ceiling, ceiling_warnings = compute_iaa_ceiling(
        labels, keys=valid_keys, ci_level=ci_level, n_resamples=n_resamples, seed=seed
    )
    valid_key_set = set(valid_keys)
    n_adjudicated = sum(
        1
        for g in gold
        if g.source == "adjudication" and (g.item_id, g.candidate, g.field) in valid_key_set
    )

    resolved_date = resolve_certificate_date(labels, date_override)

    n_perturbed: int | None = None
    achieved_fail_prevalence: float | None = None
    real_only_kappa: KappaResult | None = None
    perturbed_rows_passed_by_gold: int | None = None
    real_only_warnings: tuple[str, ...] = ()
    if has_probe_set:
        n_perturbed = len(overlay_by_key)
        achieved_fail_prevalence = 1.0 - overall.prevalence
        real_only = compute_real_only_kappa(
            paired, probe_item_ids, ci_level=ci_level, n_resamples=n_resamples, seed=seed
        )
        if real_only is not None:
            real_only_kappa, real_only_warnings = real_only
        perturbed_rows_passed_by_gold = sum(
            1
            for g in gold
            if (g.item_id, g.candidate, g.field) in overlay_by_key and g.verdict == "pass"
        )

    return CalibrationResult(
        judge_version=current_judge_version,
        label_file_hash=label_file_hash,
        date=resolved_date,
        overall=overall,
        per_candidate=per_candidate,
        verdict=verdict,
        divergence_flag=divergence,
        initial_fail_rate=initial_fail_rate,
        fail_enrichment_note=fail_enrichment_note,
        judge_errors_excluded=judge_errors_excluded,
        self_consistency=self_consistency,
        ceiling=ceiling,
        warnings=agreement_warnings + ceiling_warnings + real_only_warnings,
        n_adjudicated=n_adjudicated,
        n_perturbed=n_perturbed,
        achieved_fail_prevalence=achieved_fail_prevalence,
        real_only_kappa=real_only_kappa,
        perturbed_rows_passed_by_gold=perturbed_rows_passed_by_gold,
        probe_item_ids=frozenset(probe_item_ids),
    )


def write_judgments_jsonl(
    path: str | Path,
    *,
    judgments: Sequence[JudgmentRecord],
    self_consistency: Sequence[SelfConsistencyRecord],
    judge_version: str,
    written_at: str | None = None,
) -> None:
    """Persists one live ``eval calibrate`` run's full judge output (finding
    F2) so a later ``eval calibrate --offline`` can recompute the report +
    certificate with zero client construction. Always a full, fresh
    overwrite (mirrors ``write_certificate``) -- never an incremental append,
    since it represents one invocation's complete judged-triple set, not a
    resumable log.

    Written atomically: the full content is built in memory and written to a
    temp file in the SAME directory, flushed and fsynced, then moved into
    place with ``os.replace`` -- the same technique ``runner.py``'s
    ``_repair_truncated_tail`` uses, so a crash mid-write can never leave a
    half-written, unparseable ``judgments.jsonl`` for the next ``--offline``
    invocation to trip over (the previous, still-intact file simply survives
    untouched until the write completes).

    The first line is a ``{"kind": "meta", ...}`` header carrying
    ``judge_version``/``written_at`` (``run_calibration_offline``'s
    staleness check reads this); every subsequent line is a ``{"kind":
    "judgment", ...}`` or ``{"kind": "self_consistency", ...}`` row.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    meta = {
        "kind": "meta",
        "judge_version": judge_version,
        "written_at": written_at if written_at is not None else datetime.now(UTC).isoformat(),
    }
    lines = [json.dumps(meta, sort_keys=True)]
    for j in judgments:
        lines.append(json.dumps({"kind": "judgment", **asdict(j)}, sort_keys=True))
    for s in self_consistency:
        lines.append(json.dumps({"kind": "self_consistency", **asdict(s)}, sort_keys=True))

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    # Clean up any stale temp file from a prior killed write (same atomic-write
    # pattern as runner.py's _repair_truncated_tail).
    if tmp_path.exists():
        tmp_path.unlink()
    with tmp_path.open("w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(str(tmp_path), str(path))


def load_judgments_jsonl(path: str | Path) -> JudgmentsFile:
    """Parses a ``judgments.jsonl`` written by ``write_judgments_jsonl``
    (finding F2). Raises ``ValueError`` if the file has no ``meta`` header
    row, or an unrecognized row ``kind`` -- a corrupt/hand-edited file should
    never silently misparse into an incomplete ``JudgmentsFile``."""

    path = Path(path)
    meta: dict | None = None
    judgments: list[JudgmentRecord] = []
    self_consistency: list[SelfConsistencyRecord] = []

    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            kind = row.pop("kind", None)
            if kind == "meta":
                meta = row
            elif kind == "judgment":
                judgments.append(JudgmentRecord(**row))
            elif kind == "self_consistency":
                self_consistency.append(SelfConsistencyRecord(**row))
            else:
                raise ValueError(f"unrecognized judgments.jsonl row kind {kind!r} in {path}")

    if meta is None:
        raise ValueError(f"{path} has no meta/header row (judge_version, written_at)")

    return JudgmentsFile(
        judge_version=meta["judge_version"],
        written_at=meta["written_at"],
        judgments=tuple(judgments),
        self_consistency=tuple(self_consistency),
    )


# --------------------------------------------------------------------------
# Small I/O helpers (the CLI composes these; kept here so calibrate.py owns
# its own file format, mirroring gate/baseline.py's mix of pure logic +
# small, explicit I/O helpers).
# --------------------------------------------------------------------------


def load_calibration_labels(path: str | Path) -> list[CalibrationLabel]:
    """Parses ``data/calibration/labels.jsonl`` into ``CalibrationLabel``s."""

    labels: list[CalibrationLabel] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            labels.append(CalibrationLabel.model_validate(json.loads(line)))
    return labels


def hash_label_file(path: str | Path) -> str:
    """sha256 of the raw bytes of ``labels.jsonl`` -- ``Certificate.
    label_file_hash`` (spec §5): ties a certificate to the exact label
    content that produced it."""

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def resolve_certificate_date(
    labels: Sequence[CalibrationLabel], explicit: date | None = None
) -> date:
    """``explicit`` when given; otherwise the most recent ``label_date``
    across ALL labels (any round) -- never wall-clock-dependent (spec/ticket:
    the certificate date must be reproducible from the label file alone).
    Raises ``ValueError`` if neither is available."""

    if explicit is not None:
        return explicit
    if not labels:
        raise ValueError("cannot resolve a certificate date: no labels and no explicit date given")
    return max(label.label_date for label in labels)


def write_certificate(certificate: Certificate, path: str | Path) -> None:
    """Writes ``certificate`` to ``path`` (``data/calibration/certificate.json``),
    creating parent directories as needed."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(certificate.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
    )


__all__ = [
    "ADEQUACY_KAPPA_THRESHOLD",
    "DEFAULT_SELF_CONSISTENCY_N",
    "DEFAULT_SELF_CONSISTENCY_REPEATS",
    "DIVERGENCE_GAP_THRESHOLD",
    "GRAY_ZONE_CI_LOWER_THRESHOLD",
    "OWNER_ANNOTATOR",
    "STRATIFICATION_FAIL_RATE_THRESHOLD",
    "CalibrationBindingError",
    "CalibrationResult",
    "DualAnnotationError",
    "DualAnnotatorCoverage",
    "GoldLabel",
    "JudgedTriple",
    "JudgmentRecord",
    "JudgmentsFile",
    "PairedJudgment",
    "PerturbationOverlayError",
    "SelfConsistencyRecord",
    "SelfConsistencyResult",
    "StaleJudgmentsError",
    "Triple",
    "apply_perturbation_overlay",
    "build_certificate",
    "build_triples",
    "check_disjoint_from_golden",
    "compute_agreement",
    "compute_iaa_ceiling",
    "compute_real_only_kappa",
    "decide_verdict",
    "hash_label_file",
    "hash_output",
    "judge_triples",
    "judgment_records_from_judged",
    "labeling_template_rows",
    "load_calibration_labels",
    "load_judgments_jsonl",
    "load_perturbation_overlay",
    "measure_self_consistency",
    "pair_judgments_with_labels",
    "pair_with_labels",
    "per_candidate_divergence_flag",
    "render_calibration_report",
    "resolve_certificate_date",
    "resolve_gold_labels",
    "run_calibration",
    "run_calibration_offline",
    "select_fixed_self_consistency_triples",
    "validate_perturbation_overlay",
    "write_certificate",
    "write_judgments_jsonl",
]
