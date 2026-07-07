"""Baseline artifact I/O, generation, and fingerprint checking (spec §7, D3).

A baseline (``baselines/{candidate}.json``, committed) is the frozen
reference an ``eval gate`` run compares against: per-item, per-replicate,
per-field scores for the whole golden set, the raw model/judge outputs that
produced them, the spec §7 fingerprint (both as one opaque hash and as its
individual raw components, so a mismatch can be reported field-by-field), and
the measured adversarial-guardrail noise floor (D3's coarse guardrail must be
verified against real run-to-run variance *at baseline time*, not gate time).

**Fake-client/unit-test only (this ticket, T15):** real, traced, committed
baselines are generated in T16 after T12-T14 land (a real calibration
certificate and composite-mode decision must exist first). This module only
exposes ``generate_baseline`` as a library callable -- baselines are never
auto-created by the gate itself (spec §7's exit-2 contract: a missing
baseline fails the gate with a re-baseline instruction); CLI wiring
(``eval gate --update-baseline``) lands in T16.

**Raw-outputs storage: embedded, not a pointer.** The ticket allows either
"raw model and judge outputs" embedded directly, or "a pointer to their
committed location" -- provided the artifact stays self-sufficient for the
gate's paired-delta computation. Spec §7 explicitly settles this for this
codebase: ``baselines/`` is described as holding "per-field, per-replicate,
raw outputs" directly, and it is committed alongside ``results/published/``
as the *only* stored run data (the constitution's run-history cut means
``results/runs/`` -- where the intermediate ``run_eval`` scratch directory
this module drives lives -- is never committed and may be cleaned up at any
time). A pointer into that ephemeral directory would silently stop being
self-sufficient the moment it's cleaned up; embedding is the only choice that
keeps a committed ``baselines/{candidate}.json`` inspectable on its own
forever. Rows are stored as ``runner.RunRow`` verbatim (reused, not
duplicated) -- it already carries exactly this shape (per-field scores, raw
candidate output, raw per-field judge output, per-field judge rationales).

**Interface evolution beyond the ticket's literal block (documented here,
same convention as ``runner.py``'s module docstring for T08):**

- ``generate_baseline(config, model_key) -> BaselineFile`` is the ticket's
  literal signature. This module adds ``dataset`` (required, keyword-only --
  the golden set to score; mirrors ``run_eval``'s own explicit ``dataset``
  parameter rather than have this module duplicate the CLI's dataset-file
  loading), plus ``prompt``/``composite_mode``/``calibration_verdict``
  (keyword-only, defaulted) so a caller with a real certificate and
  composite-mode decision in hand (T16) can supply the real values while
  this ticket's fake-client tests can rely on sane, uncalibrated defaults --
  and ``runs_root``/``baselines_root`` (keyword-only, defaulted) purely for
  test isolation under ``tmp_path``, exactly as ``run_eval``'s own
  ``runs_root``/``max_workers`` were added beyond its ticket's literal
  signature.
- ``check_fingerprint(baseline, run) -> list[Mismatch]`` takes a
  ``FingerprintComponents`` as its ``run`` side, not a bare ``RunArtifact``.
  ``runner.py``'s own module docstring explains why: a ``RunArtifact``'s
  ``fingerprint`` is baked with *placeholder* ``composite_mode``/
  ``calibration_verdict`` (T08 has no certificate or composite-mode decision
  to draw on at run-persist time), so it cannot supply real values for
  either -- "T15/T16 ... should recompute their own comparison fingerprint
  from RunArtifact's raw components ... rather than trust this one for gate
  purposes." ``fingerprint_components_from_run`` is the supported way to do
  that recomputation once the real values are known (T16, from the
  certificate and composite-mode decision).
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from harness.config import Config, fingerprint
from harness.prompts import EXTRACTION_PROMPT, PromptTemplate
from harness.runner import DEFAULT_RUNS_ROOT, ModelKey, RunArtifact, RunRow, load_run, run_eval
from harness.schema import GoldenItem
from harness.scoring.composite import DETERMINISTIC_FIELDS, JUDGED_FIELDS, CompositeMode, composite

BASELINE_SCHEMA_VERSION = 1
DEFAULT_BASELINES_ROOT = Path("baselines")

# Spec §7's adversarial guardrail: a fixed, non-statistical threshold (never
# a config value -- spec calls it "a deterministic threshold", distinct from
# the tunable D3 gate.margin/gate.alpha) that must be verified >= this many
# times the measured run-to-run SE, at baseline generation time.
GUARDRAIL_THRESHOLD_POINTS = 10.0
GUARDRAIL_SE_MULTIPLIER = 3.0

# Locally derived from the public field-group constants (never the private
# mapping in scoring/composite.py) so this can never drift from them.
_MODE_FIELDS: dict[CompositeMode, tuple[str, ...]] = {
    CompositeMode.FULL_7: DETERMINISTIC_FIELDS + JUDGED_FIELDS,
    CompositeMode.DETERMINISTIC_5: DETERMINISTIC_FIELDS,
}


@dataclass(frozen=True)
class FingerprintComponents:
    """The raw spec §7 fingerprint inputs for one side of a baseline/run
    comparison -- not just the single opaque hash ``config.fingerprint``
    produces. ``check_fingerprint`` compares these field-by-field so a
    mismatch is always reported by name, never just "fingerprint differs".
    """

    prompt_version: int
    dataset_version: int
    served_versions: dict[str, str]
    judge_version: str
    composite_mode: str
    calibration_verdict: str


@dataclass(frozen=True)
class Mismatch:
    """One differing fingerprint component between a baseline and a run,
    named so the gate's exit-2 measurement-error message (spec §7) can say
    exactly what changed instead of just "fingerprint mismatch"."""

    field: str
    baseline_value: object
    run_value: object


@dataclass(frozen=True)
class BaselineFile:
    """The parsed contents of ``baselines/{candidate}.json`` -- everything
    spec §7 requires: per-item, per-replicate, per-field scores and raw
    outputs (``items`` + ``rows``, ``RunRow`` reused verbatim), the config
    fingerprint (both as the opaque hash and as ``fingerprint_components``
    for field-by-field comparison), ``k_baseline``, and the measured
    adversarial-guardrail noise floor."""

    schema_version: int
    label: str
    k_baseline: int
    items: tuple[GoldenItem, ...]
    rows: tuple[RunRow, ...]
    fingerprint: str
    fingerprint_components: FingerprintComponents
    adversarial_noise_floor_se: float
    created_at: str


class BaselineFormatError(Exception):
    """Raised by ``load_baseline`` when a baseline file's ``schema_version``
    is missing or does not match ``BASELINE_SCHEMA_VERSION`` -- a v0/legacy
    or otherwise unrecognized baseline format. Fails loudly, naming the
    exact problem, rather than attempting a silent partial parse."""

    def __init__(self, path: Path | str, found_version: object) -> None:
        super().__init__(
            f"Unrecognized baseline format at {path}: schema_version={found_version!r}, "
            f"expected {BASELINE_SCHEMA_VERSION} (v0/legacy or corrupt baseline file -- "
            "regenerate with generate_baseline)."
        )
        self.path = path
        self.found_version = found_version


def fingerprint_components_from_run(
    run: RunArtifact, *, composite_mode: CompositeMode, calibration_verdict: str
) -> FingerprintComponents:
    """Build a comparable ``FingerprintComponents`` from a completed run's
    ``RunArtifact`` plus the real ``composite_mode``/``calibration_verdict``
    a caller (T16) has in hand at gate time -- the recomputation the module
    docstring describes, since ``RunArtifact.fingerprint`` itself is baked
    with placeholders for exactly those two fields."""

    return FingerprintComponents(
        prompt_version=run.prompt_version,
        dataset_version=run.dataset_version,
        served_versions=dict(run.served_versions),
        judge_version=run.judge_version,
        composite_mode=str(composite_mode),
        calibration_verdict=str(calibration_verdict),
    )


def check_fingerprint(baseline: BaselineFile, run: FingerprintComponents) -> list[Mismatch]:
    """Compare ``baseline``'s recorded fingerprint components against
    ``run``'s, field by field. Empty on a full match. Every differing field
    is named (spec §7: prompt version, dataset version, resolved/served
    model versions -- one entry per differing key, e.g.
    ``served_versions.judge`` -- judge version, composite definition,
    calibration verdict). A key present on only one side is fail-closed: it
    always counts as a mismatch, never as silently equal-by-absence."""

    base = baseline.fingerprint_components
    mismatches: list[Mismatch] = []

    if base.prompt_version != run.prompt_version:
        mismatches.append(Mismatch("prompt_version", base.prompt_version, run.prompt_version))
    if base.dataset_version != run.dataset_version:
        mismatches.append(Mismatch("dataset_version", base.dataset_version, run.dataset_version))
    if base.judge_version != run.judge_version:
        mismatches.append(Mismatch("judge_version", base.judge_version, run.judge_version))
    if base.composite_mode != run.composite_mode:
        mismatches.append(Mismatch("composite_mode", base.composite_mode, run.composite_mode))
    if base.calibration_verdict != run.calibration_verdict:
        mismatches.append(
            Mismatch("calibration_verdict", base.calibration_verdict, run.calibration_verdict)
        )

    all_keys = sorted(set(base.served_versions) | set(run.served_versions))
    for key in all_keys:
        base_value = base.served_versions.get(key)
        run_value = run.served_versions.get(key)
        if base_value != run_value:
            mismatches.append(Mismatch(f"served_versions.{key}", base_value, run_value))

    return mismatches


def check_guardrail_floor(
    baseline: BaselineFile,
    *,
    threshold_points: float = GUARDRAIL_THRESHOLD_POINTS,
    se_multiplier: float = GUARDRAIL_SE_MULTIPLIER,
) -> bool:
    """True iff the guardrail threshold is verified far enough above
    measured run noise (spec §7, D3): ``threshold_points >= se_multiplier *
    baseline.adversarial_noise_floor_se``. This is a baseline-time
    structural check on the *recorded* noise floor -- distinct from the
    gate's own, separate, gate-time question of whether a live run's
    adversarial delta actually trips the threshold."""

    return threshold_points >= se_multiplier * baseline.adversarial_noise_floor_se


def _adversarial_replicate_composites(
    items: Sequence[GoldenItem], rows: Sequence[RunRow], mode: CompositeMode
) -> list[float]:
    """One composite per replicate index: the mean, across adversarial
    items only, of that replicate's composite score. A row with a missing
    judged field (verdict ``None`` -- a judge error, never a fail, spec §7)
    is excluded from its replicate's mean, mirroring the gate's own
    missing-field exclusion from paired deltas."""

    adversarial_ids = {item.id for item in items if item.meta.slice == "adversarial"}
    included_fields = _MODE_FIELDS[mode]

    by_replicate: dict[int, list[float]] = {}
    for row in rows:
        if row.item_id not in adversarial_ids:
            continue
        if any(row.field_scores[f] is None for f in included_fields):
            continue
        by_replicate.setdefault(row.replicate, []).append(composite(row.field_scores, mode))

    return [statistics.mean(by_replicate[r]) for r in sorted(by_replicate)]


def _measure_adversarial_noise_floor(
    items: Sequence[GoldenItem], rows: Sequence[RunRow], mode: CompositeMode
) -> float:
    """The adversarial-slice composite's run-to-run standard error, measured
    across the baseline's own K_baseline replicates (spec §7, D3): each
    replicate's adversarial-slice composite is treated as one independent
    sample of what a fresh run would report, and the standard error of their
    mean is this baseline's noise floor -- verified against the fixed
    10-point guardrail threshold via ``check_guardrail_floor``, at baseline
    time, never at gate time.

    Raises ``ValueError`` if fewer than two replicate-level composites are
    measurable (no adversarial items in ``items``, or too many/all
    replicates excluded by a missing judged field) -- a standard error needs
    at least two samples, and silently returning e.g. 0.0 for zero samples
    would misrepresent "unmeasured" as "measured, and zero".
    """

    per_replicate = _adversarial_replicate_composites(items, rows, mode)
    if len(per_replicate) < 2:
        raise ValueError(
            "need at least 2 replicate-level adversarial composites to measure a standard "
            f"error; got {len(per_replicate)} (check the dataset has adversarial items and "
            "k_baseline > 1, and that they are not all excluded by missing judged fields)"
        )
    return statistics.stdev(per_replicate) / math.sqrt(len(per_replicate))


def _baseline_to_dict(baseline: BaselineFile) -> dict:
    return {
        "schema_version": baseline.schema_version,
        "label": baseline.label,
        "k_baseline": baseline.k_baseline,
        "items": [item.model_dump(mode="json") for item in baseline.items],
        "rows": [asdict(row) for row in baseline.rows],
        "fingerprint": baseline.fingerprint,
        "fingerprint_components": asdict(baseline.fingerprint_components),
        "adversarial_noise_floor_se": baseline.adversarial_noise_floor_se,
        "created_at": baseline.created_at,
    }


def _baseline_from_dict(data: dict, *, source: Path | str) -> BaselineFile:
    schema_version = data.get("schema_version")
    if schema_version != BASELINE_SCHEMA_VERSION:
        raise BaselineFormatError(source, schema_version)

    items = tuple(GoldenItem.model_validate(item) for item in data["items"])
    rows = tuple(RunRow(**row) for row in data["rows"])
    fingerprint_components = FingerprintComponents(**data["fingerprint_components"])

    return BaselineFile(
        schema_version=schema_version,
        label=data["label"],
        k_baseline=data["k_baseline"],
        items=items,
        rows=rows,
        fingerprint=data["fingerprint"],
        fingerprint_components=fingerprint_components,
        adversarial_noise_floor_se=data["adversarial_noise_floor_se"],
        created_at=data["created_at"],
    )


def load_baseline(path: str | Path) -> BaselineFile:
    """Parse a committed baseline JSON file into a ``BaselineFile``. Raises
    ``BaselineFormatError`` -- loudly, naming the format problem -- on a
    v0/legacy or otherwise unrecognized ``schema_version``; never a silent
    misparse or partial load."""

    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    return _baseline_from_dict(data, source=path)


def _write_baseline(baseline: BaselineFile, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_baseline_to_dict(baseline), indent=2), encoding="utf-8")


def generate_baseline(
    config: Config,
    model_key: ModelKey,
    *,
    dataset: Sequence[GoldenItem],
    prompt: PromptTemplate = EXTRACTION_PROMPT,
    composite_mode: CompositeMode = CompositeMode.FULL_7,
    calibration_verdict: str = "uncalibrated",
    runs_root: str | Path = DEFAULT_RUNS_ROOT,
    baselines_root: str | Path = DEFAULT_BASELINES_ROOT,
) -> BaselineFile:
    """Generate (and write) the baseline for ``model_key``'s candidate over
    ``dataset``, at ``K = config.k_baseline`` replicates per item (spec §7:
    K_baseline=6 keeps baseline noise small). Drives the existing
    ``run_eval``/``load_run`` machinery (T08) rather than re-implementing the
    scoring/judge/persist loop -- baseline generation is exactly that loop at
    a larger K, repackaged.

    Writes the result to ``baselines_root / f"{model_key.label}.json"``
    (overwriting any existing file there -- baseline generation is always a
    fresh, deliberate act, never a resumable append) and also returns the
    full in-memory ``BaselineFile`` (unlike ``run_eval``'s handle-only
    ``RunDir`` return -- a baseline's ~300 rows are small enough that eager,
    whole-object return costs nothing and saves every caller, including this
    ticket's tests, a round trip through disk just to inspect what was
    generated).

    Measures and records the adversarial-guardrail noise floor from this
    same K_baseline run (spec §7, D3) -- always, regardless of whether it
    would pass ``check_guardrail_floor``; that check is a separate, explicit
    step (mirroring how a fingerprint mismatch is enumerated, not
    auto-corrected).

    ``composite_mode``/``calibration_verdict`` default to the same
    uncalibrated/``FULL_7`` placeholders ``run_eval``'s own manifest uses,
    appropriate for this ticket's fake-client tests; T16's real baseline
    generation (after a certificate exists) supplies the real values.
    """

    run_dir = run_eval(
        config,
        model_key,
        k=config.k_baseline,
        dataset=dataset,
        prompt=prompt,
        runs_root=runs_root,
    )
    run_artifact = load_run(run_dir)

    fingerprint_components = fingerprint_components_from_run(
        run_artifact, composite_mode=composite_mode, calibration_verdict=calibration_verdict
    )
    effective_config = config.model_copy(update={"prompt_version": prompt.version})
    baseline_fingerprint = fingerprint(
        effective_config,
        fingerprint_components.served_versions,
        fingerprint_components.judge_version,
        fingerprint_components.composite_mode,
        fingerprint_components.calibration_verdict,
    )
    noise_floor_se = _measure_adversarial_noise_floor(
        run_artifact.items, run_artifact.rows, composite_mode
    )

    baseline = BaselineFile(
        schema_version=BASELINE_SCHEMA_VERSION,
        label=model_key.label,
        k_baseline=config.k_baseline,
        items=run_artifact.items,
        rows=run_artifact.rows,
        fingerprint=baseline_fingerprint,
        fingerprint_components=fingerprint_components,
        adversarial_noise_floor_se=noise_floor_se,
        created_at=datetime.now(UTC).isoformat(),
    )

    _write_baseline(baseline, Path(baselines_root) / f"{model_key.label}.json")
    return baseline
