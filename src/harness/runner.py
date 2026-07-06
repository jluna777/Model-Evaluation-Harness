"""Run loop: items x replicates x candidates, persisted incrementally, resumable (spec §6, §9).

``run_eval`` drives one candidate over a dataset for ``k`` replicates each,
writing one JSONL row per completed ``(item, replicate)`` pair as soon as it
finishes -- never buffered until the end -- so an interrupted run can be
resumed by calling ``run_eval`` again with the *same* arguments: the run
directory is derived deterministically from the call's own inputs (model key,
dataset content, k, prompt version, dataset version), so an identical
invocation always maps back to the same directory and never re-invokes a
client for an ``(item, replicate)`` that already has a persisted row (spec
§9's "a returned response is never re-sampled", extended across runs).

**Design decisions beyond the ticket's literal interfaces (documented here
because nothing upstream pins them):**

- ``ModelKey`` bundles the candidate label ("a"/"b") with the two concrete
  ``ModelClient`` implementations a run needs (candidate + judge). The
  ticket's signature is ``run_eval(config, model_key, *, k, dataset,
  prompt)`` with no separate client parameter, and this module must not
  import provider SDKs (that stays in ``models/*_client.py`` and the T11
  CLI, which constructs real clients "only when a live run is required").
  Bundling both clients into the object passed as ``model_key`` is the
  smallest way to satisfy both constraints; tests construct ``ModelKey``
  directly with fakes, no SDK or transport mocking needed.
- ``runs_root`` and ``max_workers`` are additional *keyword-only, defaulted*
  parameters appended after the ticket's documented ones. They don't change
  the call shape for any caller that only uses the documented arguments;
  they exist so tests can redirect output under ``tmp_path`` and control
  concurrency for deterministic ordering, without the run loop reaching into
  test-only global state.
- Bounded concurrency uses a fixed ``ThreadPoolExecutor`` of 4 workers by
  default. All three SDK clients are synchronous, and a candidate/judge call
  is network-latency-bound, not CPU-bound, so a small thread pool buys most
  of the achievable overlap without hammering per-minute provider rate
  limits harder than necessary -- 4 is a deliberately conservative, fixed
  bound (not tuned per-provider). All tasks for a run are submitted to the
  pool upfront, so a worker that finishes a task can otherwise race straight
  to the next queued one before the main thread ever inspects the failed
  future -- a shared ``threading.Event`` set the instant any task observes
  ``TransportExhausted``, and checked at the top of every task, is what
  actually stops that extra, unnecessary spend once a transport failure is
  known (a task already in flight on another worker when the event is set
  still runs to completion; only not-yet-started tasks are skipped).
- **Missing-judge-verdict representation (binding for T10/T11/T15/T16):** a
  judge error (``JudgeResult.verdict is None``) is recorded as an explicit
  ``None`` in that row's ``field_scores[field]`` -- never a dropped key,
  never coerced to ``0``. This is unambiguous because the *other* path that
  scores a field ``0`` (candidate output that is schema-invalid or a
  refusal) always scores *all seven* fields ``0`` at once and never leaves
  any of them ``None`` -- so a lone ``None`` can only mean "judged, but the
  judge call errored". ``raw_judge``/``judge_rationales`` are ``None`` for a
  field precisely when no judge call was made at all for it (the candidate
  output failed schema validation, so there was nothing to judge); when a
  judge call *was* made but errored, ``raw_judge`` still carries the judge's
  raw response text (``JudgeResult.raw`` is always populated) while
  ``judge_rationales`` is ``None`` (no rationale on error).
- Per-row ``usage``/``served_model_version`` describe the *candidate* call
  only. ``Judge``/``JudgeResult`` (T7) deliberately does not surface the
  underlying judge ``StructuredResult``'s usage or served version to
  callers, so the runner has no way to observe them through the judge's
  binding interface -- this is presumably intentional, deferring
  judge-call cost/version tracking to tracing spans (T09) rather than the
  JSONL row. Consistently, the fingerprint's ``served_versions`` mapping
  here only records the candidate's served version; the judge's identity is
  already covered by ``judge_version()`` (a hash that bakes in the pinned
  judge model id string), which is a static identity check rather than a
  runtime alias-drift observation.
- The manifest stores a fingerprint computed with placeholder
  ``composite_mode=FULL_7`` and ``calibration_verdict="uncalibrated"``,
  because T08 has no certificate (T14 doesn't exist yet) and no report-time
  composite-mode decision to draw on. This fingerprint's purpose here is
  run-identity (resume matching, "did the prompt version change") rather
  than the gate's baseline-comparison fingerprint -- T15/T16 have the real
  calibration verdict and composite mode in hand and should recompute their
  own comparison fingerprint from ``RunArtifact``'s raw components
  (``served_versions``, ``judge_version``, ``prompt_version``,
  ``dataset_version``) rather than trust this one for gate purposes.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from harness.config import Config, fingerprint
from harness.judge.judge import Judge
from harness.judge.rubric import judge_version as compute_judge_version
from harness.models import ModelClient, StructuredResult
from harness.models.retry import TransportExhausted
from harness.prompts import PromptTemplate
from harness.schema import GoldenItem, TicketExtraction
from harness.scoring.composite import DETERMINISTIC_FIELDS, JUDGED_FIELDS, CompositeMode
from harness.scoring.deterministic import score_deterministic

ALL_FIELDS: tuple[str, ...] = DETERMINISTIC_FIELDS + JUDGED_FIELDS

DEFAULT_RUNS_ROOT = Path("results/runs")
# Bounded concurrency: SDK clients are synchronous and I/O-bound; 4 is a small,
# fixed, deliberately conservative bound (see module docstring).
DEFAULT_MAX_WORKERS = 4

_MANIFEST_FILENAME = "manifest.json"
_ROWS_FILENAME = "rows.jsonl"


@dataclass(frozen=True)
class ModelKey:
    """Bundles the candidate label with the concrete clients a run needs.

    ``label`` is the fingerprint/CLI-facing candidate identifier ("a" | "b",
    matching ``eval run --model {a|b}`` and ``Config.models.candidate_a/b``).
    ``candidate_client`` speaks the extraction schema against
    ``TicketExtraction``; ``judge_client`` is the raw judge-model client --
    the runner wraps it in ``Judge`` itself, one field-judgment call at a
    time. See the module docstring for why both clients are bundled here
    instead of constructed inside this module.
    """

    label: Literal["a", "b"]
    candidate_client: ModelClient
    judge_client: ModelClient


@dataclass(frozen=True)
class RunDir:
    """Handle to a run's persisted directory. Returned by ``run_eval``,
    accepted by ``load_run``."""

    path: Path


class RunAborted(Exception):
    """Raised when ``TransportExhausted`` surfaces mid-run (spec §6): a
    measurement error, never scored, distinct from a completed run's normal
    ``RunDir`` return. The partial JSONL file already written remains on
    disk -- re-invoking ``run_eval`` with the same arguments resumes from the
    persisted rows rather than re-spending calls."""

    def __init__(self, run_dir: RunDir, cause: BaseException) -> None:
        super().__init__(f"Run aborted at {run_dir.path}: {cause!r}")
        self.run_dir = run_dir
        self.cause = cause


class RunConfigMismatch(Exception):
    """Raised when a run directory already exists but the current call's
    arguments disagree with what's recorded in its manifest -- a defensive
    guard against resuming into an incompatible run (in practice this should
    only fire on a hash collision, since the run directory name is itself
    derived from these same inputs)."""

    def __init__(self, run_dir: Path, mismatches: Sequence[str]) -> None:
        reasons = ", ".join(mismatches)
        super().__init__(f"Run dir {run_dir} is incompatible with this call: {reasons}")
        self.run_dir = run_dir
        self.mismatches = tuple(mismatches)


@dataclass(frozen=True)
class RunRow:
    """One persisted (item, replicate) outcome -- the JSONL row schema
    (binding: ``item_id``, ``replicate``, ``raw_output``, ``raw_judge``,
    ``field_scores``, ``usage``, ``served_model_version``,
    ``judge_rationales``). See the module docstring for the missing-verdict
    representation and the usage/served-version scope."""

    item_id: str
    replicate: int
    raw_output: str
    raw_judge: dict[str, str | None]
    field_scores: dict[str, int | None]
    usage: dict[str, int]
    served_model_version: str
    judge_rationales: dict[str, str | None]


@dataclass(frozen=True)
class RunArtifact:
    """Read-side contract over a persisted run directory -- the manifest plus
    ``rows.jsonl``, parsed. Consumed by reports (T10), the CLI (T11), and the
    baseline/gate modules (T15/T16).

    - ``items``: the exact ``GoldenItem``s this run scored, persisted in full
      at run time so ``eval rescore`` never needs to re-read the live dataset
      file (spec AC5: zero API calls, and no dependency on the dataset file
      still being at the same path/version later).
    - ``rows``: one ``RunRow`` per completed ``(item, replicate)``.
    - ``served_versions``/``judge_version``/``fingerprint``: this run's raw
      fingerprint inputs, plus a fingerprint computed for run-identity
      purposes (module docstring explains why it isn't the gate's real
      comparison fingerprint).
    - ``completed``: ``False`` for a run loaded mid-abort (before resuming);
      ``True`` once every ``(item, replicate)`` task has a persisted row.
    """

    run_dir: RunDir
    model_key: str
    k: int
    prompt_version: int
    dataset_version: int
    items: tuple[GoldenItem, ...]
    rows: tuple[RunRow, ...]
    served_versions: dict[str, str]
    judge_version: str
    fingerprint: str
    completed: bool

    def rows_for_item(self, item_id: str) -> tuple[RunRow, ...]:
        """All persisted replicates for one item, in persisted order."""

        return tuple(row for row in self.rows if row.item_id == item_id)

    def slice_for(self, item_id: str) -> str:
        """The ``nominal``/``adversarial`` slice tag from that item's
        ``GoldenMeta``, as persisted with this run (not re-read from the
        live dataset)."""

        for item in self.items:
            if item.id == item_id:
                return item.meta.slice
        raise KeyError(item_id)

    def judge_error_count(self) -> int:
        """Count of individual judged-field calls that errored (``verdict
        is None``) across every persisted row -- the quantity the gate's
        >5% judge-error-rate budget (spec §7) is measured against."""

        return sum(1 for row in self.rows for value in row.field_scores.values() if value is None)

    def usage_totals(self) -> dict[str, int]:
        """Summed candidate-call token usage across every persisted row."""

        totals = {"input_tokens": 0, "output_tokens": 0}
        for row in self.rows:
            totals["input_tokens"] += row.usage["input_tokens"]
            totals["output_tokens"] += row.usage["output_tokens"]
        return totals


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _run_id(
    model_key_label: str,
    items: Sequence[GoldenItem],
    k: int,
    prompt_version: int,
    dataset_version: int,
    dataset_path: str,
) -> str:
    sorted_items = sorted(items, key=lambda item: item.id)
    payload = {
        "model_key": model_key_label,
        "items": [item.model_dump(mode="json") for item in sorted_items],
        "k": k,
        "prompt_version": prompt_version,
        "dataset_version": dataset_version,
        "dataset_path": dataset_path,
    }
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return digest[:16]


def _run_dir_path(
    runs_root: Path,
    model_key_label: str,
    items: Sequence[GoldenItem],
    k: int,
    prompt_version: int,
    dataset_version: int,
    dataset_path: str,
) -> Path:
    run_id = _run_id(model_key_label, items, k, prompt_version, dataset_version, dataset_path)
    return runs_root / f"{model_key_label}-{run_id}"


def _initial_manifest(
    model_key: ModelKey,
    items: Sequence[GoldenItem],
    k: int,
    prompt: PromptTemplate,
    config: Config,
) -> dict:
    return {
        "model_key": model_key.label,
        "k": k,
        "prompt_version": prompt.version,
        "dataset_path": config.dataset.path,
        "dataset_version": config.dataset.version,
        "item_ids": [item.id for item in items],
        "items": [item.model_dump(mode="json") for item in items],
        "served_versions": {},
        "judge_version": compute_judge_version(),
        "composite_mode": str(CompositeMode.FULL_7),
        "calibration_verdict": "uncalibrated",
        "fingerprint": None,
        "completed": False,
        "created_at": datetime.now(UTC).isoformat(),
    }


def _check_manifest_compatible(
    manifest: dict,
    run_dir_path: Path,
    model_key: ModelKey,
    items: Sequence[GoldenItem],
    k: int,
    prompt: PromptTemplate,
    config: Config,
) -> None:
    mismatches: list[str] = []
    if manifest["model_key"] != model_key.label:
        mismatches.append("model_key")
    if manifest["k"] != k:
        mismatches.append("k")
    if manifest["prompt_version"] != prompt.version:
        mismatches.append("prompt_version")
    if manifest["dataset_version"] != config.dataset.version:
        mismatches.append("dataset_version")
    if sorted(manifest["item_ids"]) != sorted(item.id for item in items):
        mismatches.append("item_ids")
    if mismatches:
        raise RunConfigMismatch(run_dir_path, mismatches)


def _load_completed_keys(rows_path: Path) -> set[tuple[str, int]]:
    if not rows_path.exists():
        return set()
    keys: set[tuple[str, int]] = set()
    with rows_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            keys.add((row["item_id"], row["replicate"]))
    return keys


def _score_row(
    item: GoldenItem,
    replicate: int,
    model_key: ModelKey,
    prompt: PromptTemplate,
) -> RunRow:
    rendered = prompt.render(item.email)
    result: StructuredResult = model_key.candidate_client.complete_structured(
        rendered, TicketExtraction
    )
    usage = {"input_tokens": result.usage.input_tokens, "output_tokens": result.usage.output_tokens}

    if result.failure is not None:
        # Candidate output is schema-invalid or a refusal: a real candidate
        # failure, scored (spec §6) -- all seven fields 0, raw persisted, no
        # judge calls (there is no candidate value to judge).
        return RunRow(
            item_id=item.id,
            replicate=replicate,
            raw_output=result.raw,
            raw_judge=dict.fromkeys(JUDGED_FIELDS),
            field_scores=dict.fromkeys(ALL_FIELDS, 0),
            usage=usage,
            served_model_version=result.served_model_version,
            judge_rationales=dict.fromkeys(JUDGED_FIELDS),
        )

    output = result.output
    if not isinstance(output, TicketExtraction):
        # StructuredResult's contract (models/__init__.py): output is None iff
        # failure is set. failure is None here, so this is a ModelClient bug.
        raise AssertionError(
            f"ModelClient.complete_structured returned failure=None with output={output!r}"
        )

    field_scores: dict[str, int | None] = dict(score_deterministic(item.expected, output))
    raw_judge: dict[str, str | None] = {}
    judge_rationales: dict[str, str | None] = {}
    judge = Judge(model_key.judge_client)
    for field_name in JUDGED_FIELDS:
        judge_result = judge.judge_field(
            item.email,
            field_name,
            getattr(item.expected, field_name),
            getattr(output, field_name),
        )
        raw_judge[field_name] = judge_result.raw
        judge_rationales[field_name] = judge_result.rationale
        field_scores[field_name] = None if judge_result.verdict is None else int(
            judge_result.verdict == "pass"
        )

    return RunRow(
        item_id=item.id,
        replicate=replicate,
        raw_output=result.raw,
        raw_judge=raw_judge,
        field_scores=field_scores,
        usage=usage,
        served_model_version=result.served_model_version,
        judge_rationales=judge_rationales,
    )


def run_eval(
    config: Config,
    model_key: ModelKey,
    *,
    k: int,
    dataset: Sequence[GoldenItem],
    prompt: PromptTemplate,
    runs_root: str | Path = DEFAULT_RUNS_ROOT,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> RunDir:
    """Run one candidate over ``dataset`` for ``k`` replicates each.

    Persists one JSONL row per ``(item, replicate)`` as soon as it completes.
    Re-invoking with identical arguments (including ``dataset`` content)
    resumes at the first missing ``(item, replicate)`` pair without
    re-calling clients for rows that already exist on disk. Raises
    ``RunAborted`` (never returns) if ``TransportExhausted`` surfaces from
    any candidate or judge call -- the partial file stays intact.
    """

    items = list(dataset)
    runs_root = Path(runs_root)
    run_dir_path = _run_dir_path(
        runs_root,
        model_key.label,
        items,
        k,
        prompt.version,
        config.dataset.version,
        config.dataset.path,
    )
    run_dir = RunDir(path=run_dir_path)
    run_dir_path.mkdir(parents=True, exist_ok=True)

    manifest_path = run_dir_path / _MANIFEST_FILENAME
    rows_path = run_dir_path / _ROWS_FILENAME

    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _check_manifest_compatible(manifest, run_dir_path, model_key, items, k, prompt, config)
    else:
        manifest = _initial_manifest(model_key, items, k, prompt, config)
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    completed = _load_completed_keys(rows_path)
    tasks = [
        (item, replicate)
        for item in items
        for replicate in range(k)
        if (item.id, replicate) not in completed
    ]

    served_versions: dict[str, str] = dict(manifest.get("served_versions") or {})
    served_versions_lock = threading.Lock()
    write_lock = threading.Lock()
    # Set the instant any task observes TransportExhausted, and checked at the
    # top of every task. Submitting the whole task list upfront (below) means a
    # worker thread can otherwise race straight past a failing task to the next
    # one in its queue before the main thread ever calls `future.result()` on
    # the failed future -- this flag is what actually stops that additional,
    # unnecessary spend once a transport failure is known.
    abort_event = threading.Event()

    def _run_and_write(item: GoldenItem, replicate: int) -> None:
        if abort_event.is_set():
            return
        try:
            row = _score_row(item, replicate, model_key, prompt)
        except TransportExhausted:
            abort_event.set()
            raise
        with served_versions_lock:
            served_versions[f"candidate_{model_key.label}"] = row.served_model_version
        with write_lock, rows_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(row)) + "\n")
            fh.flush()

    def _write_final_manifest(*, completed_flag: bool) -> None:
        effective_config = config.model_copy(update={"prompt_version": prompt.version})
        final_manifest = dict(manifest)
        final_manifest["served_versions"] = dict(served_versions)
        final_manifest["completed"] = completed_flag
        final_manifest["fingerprint"] = fingerprint(
            effective_config,
            served_versions,
            final_manifest["judge_version"],
            final_manifest["composite_mode"],
            final_manifest["calibration_verdict"],
        )
        manifest_path.write_text(json.dumps(final_manifest, indent=2), encoding="utf-8")

    if tasks:
        executor = ThreadPoolExecutor(max_workers=max_workers)
        futures = {executor.submit(_run_and_write, item, replicate) for item, replicate in tasks}
        try:
            for future in as_completed(futures):
                future.result()
        except TransportExhausted as exc:
            executor.shutdown(wait=True, cancel_futures=True)
            _write_final_manifest(completed_flag=False)
            raise RunAborted(run_dir, exc) from exc
        executor.shutdown(wait=True)

    _write_final_manifest(completed_flag=True)
    return run_dir


def load_run(run_dir: RunDir) -> RunArtifact:
    """Parse a persisted run directory (manifest + JSONL rows) into a
    ``RunArtifact``. Works on both completed and aborted (partial) runs --
    check ``.completed`` to distinguish them."""

    manifest_path = run_dir.path / _MANIFEST_FILENAME
    rows_path = run_dir.path / _ROWS_FILENAME

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = tuple(GoldenItem.model_validate(item) for item in manifest["items"])

    rows: list[RunRow] = []
    if rows_path.exists():
        with rows_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rows.append(RunRow(**json.loads(line)))

    return RunArtifact(
        run_dir=run_dir,
        model_key=manifest["model_key"],
        k=manifest["k"],
        prompt_version=manifest["prompt_version"],
        dataset_version=manifest["dataset_version"],
        items=items,
        rows=tuple(rows),
        served_versions=dict(manifest["served_versions"]),
        judge_version=manifest["judge_version"],
        fingerprint=manifest["fingerprint"],
        completed=manifest["completed"],
    )
