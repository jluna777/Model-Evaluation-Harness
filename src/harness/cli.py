"""Typer CLI: ``run`` / ``compare`` / ``gate`` / ``calibrate`` / ``rescore``
(spec §9, tickets T11/T14).

This module is the composition root for Phase A: it is the primary place that
constructs real provider SDK clients (``AnthropicClient``/``OpenAIClient``/
``GeminiClient``, bundled into a ``runner.ModelKey`` -- see ``runner.py``'s
module docstring for why the runner itself never imports provider SDKs) and
the only place that decides whether a run is *reportable* (spec §8) from the
``--dataset`` flag.

**``eval calibrate`` (T14):** always reportable when run live (spec §5/§8:
judge calibration must be traceable) and never accepts a ``--dataset``
override -- its item source is always ``--emails`` (default
``data/calibration/emails.jsonl``). It reuses the same ``_get_or_run`` seam
``run``/``compare`` use to obtain each candidate's persisted calibration
``RunArtifact`` (``harness.calibrate``'s module docstring explains why this,
rather than a parallel pipeline, is the design), then constructs a SECOND,
judge-only client seam (``_build_judge_client``) to re-judge those artifacts'
triples with the CURRENT judge -- calibrate has no use for a candidate client
of its own once each candidate's run is in hand, whether that run was just
freshly executed or reused from disk. All of the actual statistics/decision
logic (agreement, verdict, self-consistency, gold resolution, the
human-human agreement ceiling, the certificate) lives in ``harness.
calibrate``; this module is CLI plumbing only
(dataset/label loading, tracing, client construction, writing the
certificate/judgments file, printing the report).

Three T14 review findings, distinct from ``eval gate``'s own F1-F3 below:
(F1) every candidate/judge client this command's live path constructs is
still routed through the usual ``_get_or_run``/``_build_judge_client`` seams,
but ``harness.calibrate.pair_with_labels`` now verifies each label's
``output_sha256`` against the candidate output it is actually being paired
with -- ``CalibrationBindingError`` (mapped to a clean exit 1 alongside
``_clean_exit_on_expected_errors``'s existing set) if a regenerated run
directory has silently drifted the two apart. (F2) the live path persists
its full judge output to ``judgments.jsonl`` (``--judgments``, default
``data/calibration/judgments.jsonl``); ``--offline`` recomputes the report +
certificate from that file plus the labels file with ZERO client
construction and no tracing requirement at all (it makes no calls of any
kind to report on) -- see ``harness.calibrate.run_calibration_offline``.
(F3) the live path always overrides ``config.k`` to 1 for both candidate
runs (``calibrate_cfg = effective_cfg.model_copy(update={"k": 1})``):
calibration is defined at one candidate output per item, and the loaded
config's own ``k`` (3 by default) would triple spend for no benefit.

**``eval gate`` (T16):** always reportable (spec §7/§8, no ``--dataset``
override -- the golden set is the only dataset a baseline is ever compared
against) and always runs both candidates. Delegates every decision --
fingerprint check, judge-error budget, the statistical + adversarial-guardrail
decision rule, rendering -- to ``harness.gate.gate.evaluate_gate`` (pure) and,
for ``--update-baseline``, one of two impure entry points: ``update_baselines``
(the default, atomically committing both candidates together -- finding F3)
or, when ``--model {a|b}`` is also passed, ``update_baseline`` (regenerating
and committing ONLY that one candidate, atomic per-file rather than per-pair
-- D3 amendment 2026-07-16, operational: the judge provider's daily quota is
too small for one dual-candidate generation, confirmed live when a dual
``--update-baseline`` run aborted mid-quota; dual mode remains the default
and unaffected when ``--model`` is omitted); this module only handles CLI
plumbing (dataset/config/certificate loading, tracing, client construction
via the same ``_build_model_key``/``_get_or_run`` seams ``run``/``compare``
use, and the gate's own exit-code mapping -- see ``_gate_clean_exit``, which
differs from ``_clean_exit_on_expected_errors`` in a binding way, revised by
finding F1: every gate condition that fires before a completed measurement
exists -- including ``RunAborted``, a run-config mismatch, missing
tracing/certificate/API-key credentials, and an SDK construction failure --
is exit 2 "measurement error", not exit 1; only a failed
``--update-baseline`` guardrail check (which DID complete a real
measurement) stays exit 1. ``eval gate`` also always forces a fresh
``run_eval`` execution for every candidate (finding F2) -- it never reuses
``run``/``compare``'s persisted run directories, since a stale completed run
could silently replay scores produced by since-changed scoring code,
defeating the gate's own threat model.).

**Client-injection seam (binding for T11's tests):** ``_build_model_key`` is
the SOLE call site that ever constructs ``AnthropicClient``/``OpenAIClient``/
``GeminiClient``. ``run``/``compare`` funnel every client construction
through it; ``rescore`` never calls it at all. Two distinct test techniques
back two distinct claims:

- Tests proving a fake candidate/judge was genuinely exercised (a real run
  happened) monkeypatch ``_build_model_key`` itself, returning a
  ``ModelKey`` built from hand-written fakes -- the *factory seam*.
- Tests proving NO client was ever constructed (``rescore``; ``compare``
  reusing matching run artifacts) monkeypatch the three concrete classes
  (``AnthropicClient``/``OpenAIClient``/``GeminiClient``) this module
  imports by name to raise on ``__init__`` -- a strictly stronger proof than
  spying on the seam function alone, since it still catches a hypothetical
  future bug that constructs one of these classes through some *other* code
  path.

**Run-identity reuse (``compare``, and incidentally ``run``):** before
constructing anything, ``_get_or_run`` calls ``runner.find_completed_run`` --
the public counterpart to ``run_eval``'s own deterministic run-directory
computation for this invocation's (label, items, k, prompt version, dataset
version/path, requested candidate/judge model ids) -- so the two can never
drift out of sync with each other. If a manifest already exists there with
``completed: true``, it is loaded directly and no client is ever constructed
for that candidate; otherwise ``_build_model_key`` + ``run_eval`` run (and
resume) as usual. This is what
"reuses existing run artifacts when fingerprints match; re-runs otherwise"
(spec §6) means operationally: the run directory's own name already encodes
everything a fingerprint match would require except the runtime-observed
served model versions, which only exist *after* a run completes -- there is
no way to predict those before running, so directory-identity match is the
actual reuse test, not a pre-run comparison of two not-yet-computed
fingerprints.

``eval gate`` deliberately opts OUT of this reuse (finding F2): a persisted,
completed run directory can only ever prove "these inputs were run once,
under whatever scoring code existed at the time" -- it says nothing about
whether the harness's *own* scoring/judging code has changed since, which is
exactly the kind of drift the gate exists to protect against (spec §7's
threat model explicitly includes "harness/scoring code changes"). So every
``eval gate`` invocation (plain, ``--seed-regression``, and
``--update-baseline`` alike) routes its ``run_eval`` calls through a runs_root
nested under a fresh, invocation-unique nonce directory (``_fresh_gate_runs_root``)
rather than the shared ``DEFAULT_RUNS_ROOT`` ``run``/``compare`` use -- so the
run-directory path ``_get_or_run``/``run_eval`` compute from this call's
inputs is guaranteed to not already exist, and ``find_completed_run`` (and
``run_eval``'s own resume-by-identity check) can never find anything to
reuse. This was chosen over threading a ``force_fresh`` flag into
``_get_or_run``/``run_eval``'s resume machinery itself (``runner.py``)
because it needs zero changes to that machinery: ``run``/``compare`` keep
their existing reuse behavior completely untouched, and the guarantee here
holds by construction (a fresh directory can have nothing stale in it) rather
than by teaching the resume logic a new bypass mode to get right.

**Dev vs golden dataset (spec §8):** ``--dataset <path>`` selects dev-stage,
never-reportable iteration; omitting it uses the config's own golden
``dataset.path``/``dataset.version`` and requests a reportable
``TraceContext`` (spec §8's fail-fast-without-keys contract applies). A dev
dataset has no committed version number of its own (that concept only
exists for the frozen golden set, spec §7), so this module derives one
deterministically from the dataset file's content (``_dev_dataset_version``)
-- stable across repeated invocations of the same file (needed for
``run_eval``'s resume-by-identity, C1) and guaranteed distinct from the
golden set's pinned version, so a dev run's identity can never collide with
(or be mistaken for) a golden run's.

**Certificate handling (spec §5/§8):** every command loads
``data/calibration/certificate.json`` if present (``None`` otherwise) and
hands it straight to the renderer -- ``render_run_report``/
``render_compare_report`` already implement the uncalibrated-banner and
``MissingCertificateError`` (reportable-without-a-certificate) contracts;
this module only maps that exception to a clean exit (see below). ``rescore``
always renders with ``reportable=False``: it recomputes whatever a run
already has on request, and ``reportable`` is otherwise irrelevant to
markdown content whenever a certificate is present (only the
certificate-absent branch reads it at all) -- ``rescore`` should never
demand recalibration just to reprint an existing run's numbers.

**Expected-failure exit mapping:** ``RunAborted``, ``RunConfigMismatch``,
``MissingTracingError``, ``MissingCertificateError``, and ``MissingApiKeyError``
are the enumerated "expected failure" set -- caught uniformly by
``_clean_exit_on_expected_errors`` and turned into a one-line ``stderr``
message plus exit code 1, never a traceback. Any other exception (a bad
``--config``/``--dataset`` path, a malformed dataset file, ...) is a genuine
usage bug and is left to propagate normally.

**Missing provider API keys (``MissingApiKeyError``):** ``_build_model_key``
checks each provider's required env var itself, before constructing any SDK
client, rather than letting the SDK's own construction-time exception surface
directly -- ``openai.OpenAI()`` raises ``openai.OpenAIError`` and
``genai.Client()`` raises ``ValueError`` when their key is absent, and
neither type is a stable, provider-agnostic contract worth hard-coding into
the mapping above. See ``MissingApiKeyError``'s own docstring for the
env-var names (read from the installed SDKs) and the construction-time
fallback wrap that backs this up.
"""

import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import anthropic
import openai
import typer
from dotenv import find_dotenv, load_dotenv
from google import genai
from google.genai import types as genai_types

import harness.calibrate as calibrate_module
from harness.config import Config, load_config
from harness.gate import gate as gate_module
from harness.gate.baseline import DEFAULT_BASELINES_ROOT, BaselineFile, load_baseline
from harness.judge.judge import Judge
from harness.models import ModelClient
from harness.models.anthropic_client import AnthropicClient
from harness.models.gemini_client import GeminiClient
from harness.models.openai_client import OpenAIClient
from harness.prompts import DEGRADED_DEMO_PROMPT, EXTRACTION_PROMPT, PromptTemplate
from harness.reports import (
    MissingCertificateError,
    render_compare_report,
    render_run_report,
    require_certificate,
)
from harness.runner import (
    DEFAULT_RUNS_ROOT,
    ModelKey,
    RunAborted,
    RunConfigMismatch,
    RunDir,
    find_completed_run,
    load_run,
    run_eval,
)
from harness.schema import Certificate, GoldenItem
from harness.tracing import MissingTracingError, TraceContext

app = typer.Typer(
    help="Structured-extraction eval harness: run, compare, gate, calibrate, rescore."
)


@app.callback(invoke_without_command=False)
def _load_env_callback() -> None:
    """Load .env file with override=False so real environment variables always win."""
    load_dotenv(find_dotenv(usecwd=True), override=False)

DEFAULT_CONFIG_PATH = Path("configs/default.yaml")
DEFAULT_CERTIFICATE_PATH = Path("data/calibration/certificate.json")
DEFAULT_CALIBRATION_EMAILS_PATH = Path("data/calibration/emails.jsonl")
DEFAULT_CALIBRATION_LABELS_PATH = Path("data/calibration/labels.jsonl")
DEFAULT_CALIBRATION_JUDGMENTS_PATH = Path("data/calibration/judgments.jsonl")
# Fail-probe perturbation set (D2 amendment 2026-07-10): both optional --
# an absent file at either default path means "no probe set"/"zero overlaid
# rows" respectively, reproducing pre-amendment behavior exactly (see
# harness.calibrate's module docstring, "Fail-probe perturbation set").
DEFAULT_FAIL_PROBE_EMAILS_PATH = Path("data/calibration/emails-fail-probe.jsonl")
DEFAULT_PERTURBATIONS_PATH = Path("data/calibration/perturbations.jsonl")

# Explicit per-request timeout, seconds (live-discovered hardening item,
# 2026-07-08/2026-07-17). All three provider SDKs default to read timeouts
# around 10 minutes when left unset (anthropic/openai resolve their NOT_GIVEN
# sentinel to a 600s read timeout; google-genai's HttpOptions.timeout has no
# client-level default at all without an explicit override) -- long enough
# for a hung connection to stall a run silently instead of handing control to
# retry_transport's backoff. ~100s clears realistic judge/candidate latency
# while still engaging retries promptly.
_REQUEST_TIMEOUT_SECONDS = 100.0


class CandidateLabel(StrEnum):
    """CLI-facing candidate selector -- mirrors ``ModelKey.label``."""

    a = "a"
    b = "b"


# --------------------------------------------------------------------------
# Small pure/IO helpers.
# --------------------------------------------------------------------------


def _load_dataset(path: Path) -> list[GoldenItem]:
    """Parse a golden/dev-shaped JSONL dataset file into ``GoldenItem``s."""

    items: list[GoldenItem] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            items.append(GoldenItem.model_validate(json.loads(line)))
    return items


def _dev_dataset_version(path: Path) -> int:
    """Deterministic stand-in dataset version for a dev-stage ``--dataset``
    file (module docstring): derived from the file's content so it changes
    exactly when the file does, and is guaranteed distinct from any golden
    ``dataset.version`` a config declares (those are small, hand-assigned
    integers; this is a full content-hash-derived integer)."""

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return int(digest[:8], 16)


def _resolve_dataset(
    config: Config, dataset_path: Path | None
) -> tuple[Config, list[GoldenItem], bool]:
    """Returns ``(effective_config, items, reportable)`` for this invocation.

    ``dataset_path is None`` -> the config's own golden dataset, reportable.
    ``dataset_path`` given -> that dev-stage file, never reportable, with an
    effective config whose ``dataset.path``/``dataset.version`` are
    overridden to describe *this* file (see ``_dev_dataset_version``).
    """

    if dataset_path is None:
        items = _load_dataset(Path(config.dataset.path))
        return config, items, True

    items = _load_dataset(dataset_path)
    effective_config = config.model_copy(
        update={
            "dataset": config.dataset.model_copy(
                update={
                    "path": str(dataset_path),
                    "version": _dev_dataset_version(dataset_path),
                }
            )
        }
    )
    return effective_config, items, False


def _resolve_calibration_dataset(
    config: Config, emails_path: Path
) -> tuple[Config, list[GoldenItem]]:
    """Returns ``(effective_config, items)`` for ``eval calibrate``'s
    ``--emails`` file: same content-derived ``dataset.path``/``dataset.version``
    override technique ``_resolve_dataset`` uses for a dev-stage ``--dataset``
    (``_dev_dataset_version``), so the calibration run's directory identity
    (``_get_or_run``/``run_eval``) is never confused with a golden run using
    the same config. Unlike ``_resolve_dataset``, this never implies
    ``reportable=False`` -- calibration is ALWAYS reportable regardless of
    which dataset backs it (spec §5/§8); ``reportable`` is decided by the
    caller, not by which item source was used.
    """

    items = _load_dataset(emails_path)
    effective_config = config.model_copy(
        update={
            "dataset": config.dataset.model_copy(
                update={"path": str(emails_path), "version": _dev_dataset_version(emails_path)}
            )
        }
    )
    return effective_config, items


def _load_certificate(path: Path = DEFAULT_CERTIFICATE_PATH) -> Certificate | None:
    if not path.exists():
        return None
    return Certificate.model_validate(json.loads(path.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------
# Client construction -- the sole seam (module docstring).
# --------------------------------------------------------------------------


class MissingApiKeyError(Exception):
    """Raised by ``_build_model_key`` when a provider's required API key
    environment variable is absent. Checked eagerly, before any SDK client is
    constructed, so a missing key always surfaces as this one clean, one-line
    message -- never an unmapped SDK exception racing a traceback to the
    user. Empirically: ``openai.OpenAI()`` raises ``openai.OpenAIError`` and
    ``genai.Client()`` raises ``ValueError`` at construction when their key is
    absent; neither type is part of any SDK's stable public contract, so
    catching either by name in ``_clean_exit_on_expected_errors`` would be
    brittle across SDK versions -- this eager, provider-agnostic env check is
    the real fix, not an addition to that mapping."""

    def __init__(self, provider: str, env_var: str) -> None:
        super().__init__(
            f"Missing {provider} API key: set {env_var} in the environment before running "
            "this command."
        )
        self.provider = provider
        self.env_var = env_var


class ClientConstructionError(Exception):
    """Raised when a provider SDK client construction fails with an unrelated
    error (not a missing API key, which is caught by ``_require_api_key``
    first). Reports the true exception type and message so the operator is not
    misdirected."""

    def __init__(self, provider: str, exc: Exception) -> None:
        super().__init__(
            f"failed to construct {provider} client: {type(exc).__name__}: {exc}"
        )
        self.provider = provider


def _require_api_key(provider: str, *env_vars: str) -> None:
    """Fail fast with ``MissingApiKeyError`` unless at least one of
    ``env_vars`` (a provider's SDK-accepted alternatives -- e.g. Gemini's
    ``GEMINI_API_KEY``/``GOOGLE_API_KEY``, read from the installed
    ``google-genai`` package) is set in the environment."""

    if any(os.environ.get(var) for var in env_vars):
        return
    raise MissingApiKeyError(provider, env_vars[0])


def _construct_client(
    provider: str, build: Callable[[], ModelClient]
) -> ModelClient:
    """Runs ``build`` (a provider SDK client construction) and re-raises ANY
    exception it raises as ``ClientConstructionError`` with the true cause --
    a fallback wrap behind ``_require_api_key``'s primary env check, so a
    future SDK behavior change (a new exception type, a check
    ``_require_api_key`` doesn't anticipate) still can't reintroduce an
    unmapped traceback here. Expected to essentially never fire in practice
    once ``_require_api_key`` has already passed."""

    try:
        return build()
    except Exception as exc:
        raise ClientConstructionError(provider, exc) from exc


def _build_model_key(label: str, config: Config) -> ModelKey:
    """Constructs the real ``ModelKey`` for ``label`` -- the sole place that
    bundles a candidate client with a judge client for ``run``/``compare``/
    ``gate``. See the module docstring for the two distinct test techniques
    this enables. (``_build_judge_client`` below is a second, deliberate
    construction seam for ``GeminiClient`` alone -- see its own docstring for
    why ``eval calibrate`` needs a judge client independent of this one.)

    Every required env var is checked FIRST -- both the candidate's and the
    judge's, ``_require_api_key`` -- before any SDK client is constructed for
    EITHER role. This is what turns ``openai.OpenAI()``/``genai.Client()``'s
    own unmapped construction-time exceptions (see ``MissingApiKeyError``)
    into a clean, predictable failure instead of a raced traceback:
    construction never starts at all until every key this call will need is
    confirmed present, so (for example) a present Anthropic key can never let
    Anthropic construction run ahead of a still-missing Gemini key. The
    construction calls themselves are additionally wrapped
    (``_construct_client``) as a fallback in case SDK behavior ever
    changes in a way the eager checks don't anticipate.
    """

    if label == "a":
        _require_api_key("Anthropic", "ANTHROPIC_API_KEY")
    else:
        _require_api_key("OpenAI", "OPENAI_API_KEY")
    _require_api_key("Gemini", "GEMINI_API_KEY", "GOOGLE_API_KEY")

    if label == "a":
        candidate_client: ModelClient = _construct_client(
            "Anthropic",
            lambda: AnthropicClient(
                config.models.candidate_a,
                anthropic.Anthropic(timeout=_REQUEST_TIMEOUT_SECONDS),
                max_attempts=config.retry_max_attempts,
            ),
        )
    else:
        candidate_client = _construct_client(
            "OpenAI",
            lambda: OpenAIClient(
                config.models.candidate_b,
                openai.OpenAI(timeout=_REQUEST_TIMEOUT_SECONDS),
                max_attempts=config.retry_max_attempts,
            ),
        )
    judge_client: ModelClient = _construct_client(
        "Gemini",
        lambda: GeminiClient(
            config.models.judge,
            genai.Client(
                http_options=genai_types.HttpOptions(
                    timeout=int(_REQUEST_TIMEOUT_SECONDS * 1000)
                )
            ),
            max_attempts=config.retry_max_attempts,
        ),
    )

    return ModelKey(label=label, candidate_client=candidate_client, judge_client=judge_client)


def _build_judge_client(config: Config) -> ModelClient:
    """Constructs a standalone Gemini judge client for ``eval calibrate``
    (T14) -- independent of any candidate role, unlike ``_build_model_key``'s
    bundled candidate+judge pair. Calibrate needs a fresh judge (to re-judge
    calibration triples and measure self-consistency) regardless of whether
    either candidate's calibration run is freshly executed or reused from
    disk (``_get_or_run``'s reuse path never returns a ``ModelKey`` at all),
    so it cannot get a judge client through ``_build_model_key`` without also
    depending on an unrelated candidate's API key/construction succeeding."""

    _require_api_key("Gemini", "GEMINI_API_KEY", "GOOGLE_API_KEY")
    return _construct_client(
        "Gemini",
        lambda: GeminiClient(
            config.models.judge,
            genai.Client(
                http_options=genai_types.HttpOptions(timeout=int(_REQUEST_TIMEOUT_SECONDS * 1000))
            ),
            max_attempts=config.retry_max_attempts,
        ),
    )


def _get_or_run(
    config: Config,
    label: str,
    items: list[GoldenItem],
    prompt: PromptTemplate,
    trace: TraceContext | None,
    runs_root: Path,
) -> RunDir:
    """Reuse an already-complete run for ``label`` if one exists at the
    deterministic path; otherwise construct a real client and run (which
    itself resumes from any partial rows, ``run_eval``'s own contract)."""

    existing = find_completed_run(
        config, label, k=config.k, dataset=items, prompt=prompt, runs_root=runs_root
    )
    if existing is not None:
        return existing
    model_key = _build_model_key(label, config)
    return run_eval(
        config,
        model_key,
        k=config.k,
        dataset=items,
        prompt=prompt,
        runs_root=runs_root,
        trace=trace,
    )


def _fresh_gate_runs_root(base: Path = DEFAULT_RUNS_ROOT) -> Path:
    """A per-invocation, guaranteed-unused runs_root nested under ``base``
    (finding F2): every ``eval gate`` invocation (plain, ``--seed-regression``,
    and ``--update-baseline`` alike) calls this once and threads the result
    through every ``_get_or_run``/``update_baselines`` call it makes, so the
    run-directory path those compute from this call's own inputs can never
    already exist on disk -- ``_get_or_run``'s ``find_completed_run`` reuse
    check (and ``run_eval``'s own resume-by-identity check) always miss, and
    ``eval gate`` always drives a full, fresh measurement. See the module
    docstring's "Run-identity reuse" section for why this -- not a
    ``force_fresh`` flag threaded into ``runner.py``'s resume machinery -- is
    the fix: it needs zero changes to ``run``/``compare``'s existing reuse
    behavior."""

    return base / "gate" / uuid.uuid4().hex


# --------------------------------------------------------------------------
# Expected-failure exit mapping (module docstring).
# --------------------------------------------------------------------------


@contextmanager
def _clean_exit_on_expected_errors() -> Iterator[None]:
    try:
        yield
    except (
        RunAborted,
        RunConfigMismatch,
        MissingTracingError,
        MissingCertificateError,
        MissingApiKeyError,
        ClientConstructionError,
        # T14 findings F1/F2: a calibration label bound to a different
        # candidate output than the one actually judged (live or, offline,
        # persisted in judgments.jsonl), or offline judgments.jsonl produced
        # by a since-changed judge -- both are setup/data-integrity problems
        # discovered before any statistic can be trusted, never a traceback.
        calibrate_module.CalibrationBindingError,
        calibrate_module.StaleJudgmentsError,
        # Dual-annotation upgrade (owner, 2026-07-09): the two annotators'
        # labels don't satisfy the dual-annotation precondition (wrong
        # annotator count/identity, incomplete second-annotator coverage, or
        # an unadjudicated disagreement) -- a setup/data-integrity problem,
        # never a traceback.
        calibrate_module.DualAnnotationError,
        # Fail-probe perturbation set (D2 amendment 2026-07-10): an overlay
        # row targets the original emails file, a nonexistent key, or a
        # duplicate key -- a setup/data-integrity problem, never a traceback.
        calibrate_module.PerturbationOverlayError,
    ) as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc


# Every condition that fires BEFORE a completed measurement exists to render
# a verdict on -- exit 2 (finding F1, revised from the original four
# spec-enumerated conditions alone). Note RunAborted is here (exit 2) even
# though `run`/`compare` map the very same exception to exit 1 above: spec §7
# explicitly lists "aborted run" under the gate's measurement-error bucket.
# RunConfigMismatch/MissingTracingError/MissingCertificateError/
# MissingApiKeyError/ClientConstructionError all fire strictly before any
# measurement is even attempted (a run-identity clash, missing tracing
# credentials, a missing certificate, a missing API key, or an SDK
# construction failure -- none of these ever reflect "a completed measurement
# whose verdict is fail", exit 1's other meaning) -- see gate.py's module
# docstring for the full exit-code rationale.
_GATE_MEASUREMENT_ERRORS = (
    gate_module.MissingBaselineError,
    gate_module.FingerprintMismatchError,
    gate_module.JudgeErrorBudgetExceededError,
    gate_module.NominalItemSetMismatchError,
    RunAborted,
    RunConfigMismatch,
    MissingTracingError,
    MissingCertificateError,
    MissingApiKeyError,
    ClientConstructionError,
)


@contextmanager
def _gate_clean_exit(gate_runs_root: Path, *, keep_runs: bool) -> Iterator[None]:
    """``eval gate``'s own expected-failure exit mapping (module docstring,
    finding F1): every measurement-error/setup-precondition condition above
    gets exit 2; ``GuardrailFloorError`` (``--update-baseline`` only) is the
    one exception left at exit 1, since it fires only AFTER a real baseline
    candidate has been fully measured and refused for cause -- "a completed
    measurement whose verdict is fail", not a measurement error.

    Also owns ``gate_runs_root``'s cleanup (``--keep-runs``): this fresh,
    invocation-unique directory (``_fresh_gate_runs_root``) only ever exists
    to drive THIS invocation's measurement, so it is removed once the
    invocation is done -- UNLESS ``keep_runs`` was requested, or the
    invocation ended in one of ``_GATE_MEASUREMENT_ERRORS`` (kept for
    debugging: a measurement error means no trustworthy measurement
    necessarily completed, so the partial run directory may be the only
    evidence available for diagnosing it). ``GuardrailFloorError`` is NOT a
    measurement error (module docstring: "a completed measurement whose
    verdict is fail") and a normal pass/fail return is by definition a
    completed measurement -- both still get cleaned up."""

    try:
        yield
    except _GATE_MEASUREMENT_ERRORS as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc
    except gate_module.GuardrailFloorError as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        if not keep_runs:
            shutil.rmtree(gate_runs_root, ignore_errors=True)
        raise typer.Exit(code=1) from exc
    else:
        if not keep_runs:
            shutil.rmtree(gate_runs_root, ignore_errors=True)


def _load_baseline_or_raise(label: str, path: Path) -> BaselineFile:
    if not path.exists():
        raise gate_module.MissingBaselineError(label, path)
    return load_baseline(path)


def _require_config_exists(path: Path) -> None:
    """Friendly, traceback-free check for ``calibrate``'s live-path
    ``--config`` (``CalibrateConfigOption`` docstring): unlike ``run``/
    ``compare``/``gate``'s ``ConfigOption``, this one is not declared
    ``exists=True`` at the click layer (a typo'd default would otherwise be
    rejected even under ``--offline``, which never reads it at all) -- this
    is the explicit substitute, called only on the branch that actually
    calls ``load_config``."""

    if not path.exists():
        typer.secho(f"error: config file not found: {path}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2)


# --------------------------------------------------------------------------
# Commands.
# --------------------------------------------------------------------------


DatasetOption = Annotated[
    Path | None,
    typer.Option(
        "--dataset",
        exists=True,
        help="Dev-stage dataset path. Omit to use the config's golden set (reportable).",
    ),
]
ConfigOption = Annotated[
    Path,
    typer.Option("--config", exists=True, help="Path to the run configuration YAML."),
]
# `calibrate` is the one command whose `--config` may go entirely unused
# (`--offline` never reads it, module docstring) -- click's `exists=True`
# validates even an unused default eagerly, before the command body ever
# runs, which would wrongly demand a config file `--offline` has no need
# for. `calibrate` checks existence itself (`_require_config_exists`),
# ONLY on the live path that actually calls `load_config`.
CalibrateConfigOption = Annotated[
    Path,
    typer.Option("--config", help="Path to the run configuration YAML (unused with --offline)."),
]
# Fail-probe perturbation set (D2 amendment 2026-07-10): neither uses
# `exists=True` -- absence is a valid, meaningful state (no probe set / zero
# overlaid rows), the same reasoning `CalibrateConfigOption` documents above.
# `--fail-probe-emails` is read only on the live path (candidates are run on
# it); `--perturbations` is read on BOTH live and offline (the overlay file
# itself carries no API-derived state, so re-validating it offline costs
# nothing -- module docstring).
FailProbeEmailsOption = Annotated[
    Path,
    typer.Option(
        "--fail-probe-emails",
        help="Fail-probe emails JSONL path (optional; an absent file means no probe set, "
        "current behavior unchanged). Live path only.",
    ),
]
PerturbationsOption = Annotated[
    Path,
    typer.Option(
        "--perturbations",
        help="Perturbation overlay JSONL path (optional; an absent file means zero overlaid "
        "rows). Read on both the live and --offline paths.",
    ),
]


@app.command()
def run(
    model: Annotated[CandidateLabel, typer.Option("--model", help="Candidate to run: a or b.")],
    dataset: DatasetOption = None,
    config: ConfigOption = DEFAULT_CONFIG_PATH,
) -> None:
    """Run one candidate over a dataset; writes a markdown report + JSONL run artifacts."""

    with _clean_exit_on_expected_errors():
        cfg = load_config(config)
        effective_cfg, items, reportable = _resolve_dataset(cfg, dataset)
        trace = TraceContext.for_run(effective_cfg, reportable)
        run_dir = _get_or_run(
            effective_cfg, model.value, items, EXTRACTION_PROMPT, trace, DEFAULT_RUNS_ROOT
        )
        artifact = load_run(run_dir)
        certificate = _load_certificate(DEFAULT_CERTIFICATE_PATH)
        report = render_run_report(artifact, certificate=certificate, reportable=reportable)

    report_path = run_dir.path / "report.md"
    report_path.write_text(report, encoding="utf-8")
    typer.echo(report)
    typer.echo(f"Report written to {report_path}")


@app.command()
def compare(
    dataset: DatasetOption = None,
    config: ConfigOption = DEFAULT_CONFIG_PATH,
) -> None:
    """Run both candidates (reusing matching existing runs) and render the paired report."""

    with _clean_exit_on_expected_errors():
        cfg = load_config(config)
        effective_cfg, items, reportable = _resolve_dataset(cfg, dataset)
        # Spec §8 traces per-run, not per-invocation: a single shared
        # TraceContext here would mix both candidates' spans into one trace.
        trace_a = TraceContext.for_run(effective_cfg, reportable)
        trace_b = TraceContext.for_run(effective_cfg, reportable)
        run_dir_a = _get_or_run(
            effective_cfg, "a", items, EXTRACTION_PROMPT, trace_a, DEFAULT_RUNS_ROOT
        )
        run_dir_b = _get_or_run(
            effective_cfg, "b", items, EXTRACTION_PROMPT, trace_b, DEFAULT_RUNS_ROOT
        )
        artifact_a = load_run(run_dir_a)
        artifact_b = load_run(run_dir_b)
        certificate = _load_certificate(DEFAULT_CERTIFICATE_PATH)
        report = render_compare_report(
            artifact_a, artifact_b, certificate=certificate, reportable=reportable
        )

    report_path = Path("results") / "compare" / f"{run_dir_a.path.name}__{run_dir_b.path.name}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    typer.echo(report)
    typer.echo(f"Report written to {report_path}")


@app.command()
def gate(
    update_baseline: Annotated[
        bool,
        typer.Option(
            "--update-baseline",
            help="Regenerate and commit fresh baselines for both candidates (K_baseline, "
            "traced, reportable) instead of running the decision rule. Pair with --model to "
            "regenerate only one candidate.",
        ),
    ] = False,
    model: Annotated[
        CandidateLabel | None,
        typer.Option(
            "--model",
            help="With --update-baseline, regenerate and commit only this candidate's "
            "baseline (a or b) instead of both -- for when the judge provider's daily quota "
            "cannot fit one dual-candidate baseline generation (docs/decisions.md D3, "
            "2026-07-16 amendment). Invalid without --update-baseline.",
        ),
    ] = None,
    seed_regression: Annotated[
        bool,
        typer.Option(
            "--seed-regression",
            help="Demo mode: apply DEGRADED_DEMO_PROMPT at runtime, skip the fingerprint "
            "check, and banner the output DEMO MODE.",
        ),
    ] = False,
    keep_runs: Annotated[
        bool,
        typer.Option(
            "--keep-runs",
            help="Keep this invocation's fresh results/runs/gate/<uuid> directory after the "
            "gate finishes instead of removing it. Always kept regardless of this flag when "
            "the gate exits 2 (a measurement error) -- see the exit-code note below.",
        ),
    ] = False,
    config: ConfigOption = DEFAULT_CONFIG_PATH,
) -> None:
    """CI gate decision vs the committed baseline (spec §7). Exit 0 = pass,
    1 = regression detected (or a failed ``--update-baseline`` guardrail
    check -- the one setup-adjacent failure that still completed a real
    measurement), 2 = measurement error: missing baseline, fingerprint
    mismatch, judge-error budget exceeded, a nominal item-set mismatch, an
    aborted run, or any setup precondition that fires before a measurement
    is even attempted (missing tracing/certificate/API-key credentials, an
    SDK construction failure, a run-config mismatch) -- finding F1.

    ``--update-baseline --model {a|b}`` (D3 amendment 2026-07-16, operational):
    regenerates and commits ONLY that one candidate's baseline instead of
    both -- added because the judge provider's daily quota is smaller than
    one atomic dual-candidate baseline generation (~1,200 judge calls;
    confirmed live when a dual run aborted mid-quota). Atomicity still holds,
    just re-scoped from the pair to the one file this invocation touches: the
    OTHER candidate's committed baseline is never opened, read, or written.
    Omitting ``--model`` is unchanged -- both candidates still commit
    atomically as a pair, and remains the default. ``--model`` without
    ``--update-baseline`` is a usage error (exit 1): it has no effect outside
    baseline generation.

    This invocation's own fresh ``results/runs/gate/<uuid>`` directory
    (``_fresh_gate_runs_root``) is removed once the gate finishes, since it
    exists solely to drive this one measurement -- unless ``--keep-runs`` is
    passed, or the gate exited 2 (measurement error: the directory may be
    the only evidence available for debugging what happened, so it is always
    kept then regardless of ``--keep-runs``)."""

    if update_baseline and seed_regression:
        typer.secho(
            "error: --update-baseline and --seed-regression are mutually exclusive",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    if model is not None and not update_baseline:
        typer.secho(
            "error: --model is only valid together with --update-baseline",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    # Finding F2: every eval gate invocation gets its own fresh,
    # guaranteed-unused runs_root, so it always drives a full, fresh
    # run_eval execution for both candidates -- see
    # `_fresh_gate_runs_root`'s docstring and the module docstring's
    # "Run-identity reuse" section. Computed before `_gate_clean_exit` (which
    # owns this directory's cleanup) so it exists even if nothing below ever
    # gets far enough to create it.
    gate_runs_root = _fresh_gate_runs_root(DEFAULT_RUNS_ROOT)

    outcome: gate_module.GateOutcome | None = None
    with _gate_clean_exit(gate_runs_root, keep_runs=keep_runs):
        cfg = load_config(config)
        items = _load_dataset(Path(cfg.dataset.path))
        certificate = _load_certificate(DEFAULT_CERTIFICATE_PATH)
        # Finding F5: checked immediately after certificate load, before any
        # tracing/client construction/run -- previously a missing certificate
        # was only discovered deep inside `evaluate_gate`'s call to
        # `render_gate_summary`, AFTER both candidates' runs had already
        # spent real API calls. `--update-baseline` is exempt: it has its own
        # deliberate uncalibrated-placeholder fallback (see
        # `gate_module.update_baselines`'s docstring) and is the only gate
        # path that is allowed to proceed without a committed certificate.
        if not update_baseline:
            require_certificate(certificate, reportable=True)

        if update_baseline:
            if model is not None:
                # D3 amendment 2026-07-16: single-candidate update, atomic
                # per-file rather than per-pair -- the other candidate's
                # committed baseline is never touched (`update_baseline`'s
                # own docstring). Chosen operationally when the judge
                # provider's daily quota can't fit one dual-candidate
                # generation.
                label = model.value
                baseline_trace = TraceContext.for_run(cfg, True)
                model_key = _build_model_key(label, cfg)
                baseline = gate_module.update_baseline(
                    cfg,
                    model_key,
                    dataset=items,
                    prompt=EXTRACTION_PROMPT,
                    certificate=certificate,
                    runs_root=gate_runs_root,
                    baselines_root=DEFAULT_BASELINES_ROOT,
                    trace=baseline_trace,
                )
                typer.echo(
                    f"Baseline written: {DEFAULT_BASELINES_ROOT / f'{label}.json'} "
                    f"(fingerprint {baseline.fingerprint[:12]}...)"
                )
                return

            # Spec §8 traces per-run, not per-invocation (mirrors `compare`'s
            # own trace_a/trace_b split): each candidate's baseline generation
            # gets its own trace, both validated (fail-fast, before any API
            # call) before either client is built.
            baseline_trace_a = TraceContext.for_run(cfg, True)
            baseline_trace_b = TraceContext.for_run(cfg, True)
            # Both model keys are built upfront, before any generation --
            # matches the atomicity of the commit itself (finding F3): if
            # either candidate's credentials/construction fails, nothing has
            # been generated or spent for either candidate yet.
            model_key_a = _build_model_key("a", cfg)
            model_key_b = _build_model_key("b", cfg)
            baseline_a, baseline_b = gate_module.update_baselines(
                cfg,
                model_key_a,
                model_key_b,
                dataset=items,
                prompt=EXTRACTION_PROMPT,
                certificate=certificate,
                runs_root=gate_runs_root,
                baselines_root=DEFAULT_BASELINES_ROOT,
                trace_a=baseline_trace_a,
                trace_b=baseline_trace_b,
            )
            for label, baseline in (("a", baseline_a), ("b", baseline_b)):
                typer.echo(
                    f"Baseline written: {DEFAULT_BASELINES_ROOT / f'{label}.json'} "
                    f"(fingerprint {baseline.fingerprint[:12]}...)"
                )
            return

        trace_a = TraceContext.for_run(cfg, True)
        trace_b = TraceContext.for_run(cfg, True)

        baseline_a = _load_baseline_or_raise("a", DEFAULT_BASELINES_ROOT / "a.json")
        baseline_b = _load_baseline_or_raise("b", DEFAULT_BASELINES_ROOT / "b.json")

        prompt = DEGRADED_DEMO_PROMPT if seed_regression else EXTRACTION_PROMPT
        run_dir_a = _get_or_run(cfg, "a", items, prompt, trace_a, gate_runs_root)
        run_dir_b = _get_or_run(cfg, "b", items, prompt, trace_b, gate_runs_root)
        run_a = load_run(run_dir_a)
        run_b = load_run(run_dir_b)

        outcome = gate_module.evaluate_gate(
            cfg,
            baseline_a=baseline_a,
            baseline_b=baseline_b,
            run_a=run_a,
            run_b=run_b,
            certificate=certificate,
            seed_regression=seed_regression,
        )

    assert outcome is not None
    typer.echo(outcome.rendered)
    raise typer.Exit(code=outcome.exit_code)


@app.command()
def calibrate(
    offline: Annotated[
        bool,
        typer.Option(
            "--offline",
            help="Recompute the report + certificate purely from data/calibration/"
            "judgments.jsonl (a prior live run's persisted judge output) and the labels "
            "file -- zero API calls, zero client construction (finding F2). Fails loudly "
            "if judgments.jsonl is stale against the current judge, or misaligned with "
            "labels.jsonl.",
        ),
    ] = False,
    date_override: Annotated[
        str | None,
        typer.Option(
            "--date",
            help="Override the certificate date (YYYY-MM-DD); default: the most recent "
            "label_date in the labels file.",
        ),
    ] = None,
    emails: Annotated[
        Path, typer.Option("--emails", help="Calibration emails JSONL path.")
    ] = DEFAULT_CALIBRATION_EMAILS_PATH,
    labels_path: Annotated[
        Path, typer.Option("--labels", help="Calibration labels JSONL path.")
    ] = DEFAULT_CALIBRATION_LABELS_PATH,
    judgments_path: Annotated[
        Path,
        typer.Option(
            "--judgments",
            help="Persisted judge-results JSONL path: a live run writes this; --offline "
            "reads it.",
        ),
    ] = DEFAULT_CALIBRATION_JUDGMENTS_PATH,
    fail_probe_emails: FailProbeEmailsOption = DEFAULT_FAIL_PROBE_EMAILS_PATH,
    perturbations_path: PerturbationsOption = DEFAULT_PERTURBATIONS_PATH,
    config: CalibrateConfigOption = DEFAULT_CONFIG_PATH,
) -> None:
    """Judge calibration: agreement report + committed certificate (spec §5).

    Dual-annotation (owner-approved upgrade, 2026-07-09): both automatic, no
    flag required. Whenever ``labels_path`` carries ``round="initial"`` rows
    from exactly two annotators (``"owner"`` plus one other, covering the
    exact same keys), this command resolves the final gold labels
    (``harness.calibrate.resolve_gold_labels`` -- owner adjudication wins
    every disagreement) and computes the human-human agreement (IAA) ceiling
    (``compute_iaa_ceiling``) alongside judge agreement. Missing/incomplete
    second-annotator coverage, an adjudication row that is not the owner's,
    an adjudication row on an already-agreed key or outside the shared key
    set, or a disagreement with no adjudication row, all raise
    ``DualAnnotationError`` -- mapped to a clean exit 1 by
    ``_clean_exit_on_expected_errors``, never a traceback. This gold
    resolution runs immediately after the labels file is loaded, before the
    ``MissingTracingError`` check below and any candidate/judge client
    construction -- a labels-file defect is computable from ``labels`` alone,
    so it costs zero API calls and no client setup.

    Fail-probe perturbation set (D2 amendment 2026-07-10): ``--fail-probe-
    emails`` (live only) is loaded when the file exists at all -- an absent
    file (the default when the owner hasn't drafted a probe set) means no
    probe set, reproducing pre-amendment behavior exactly. When present,
    both candidates are run on it (same k=1 seam) and its triples are
    concatenated with the real calibration triples before judging.
    ``--perturbations`` (both live and ``--offline``) is the committed
    overlay file; an absent file means zero overlaid rows. Overlay
    validation errors (``PerturbationOverlayError``: a key targeting the
    original emails file, a nonexistent key, or a duplicate key) are mapped
    to a clean exit 1, same as every other expected-failure condition here.

    Live (default): always reportable -- fails fast with ``MissingTracingError``
    if Langfuse credentials are absent, before any candidate or judge client is
    constructed -- and persists its judge output to ``judgments.jsonl``
    (finding F2) so a later ``--offline`` run can recompute for free. Both
    candidates are always run at K=1 regardless of ``config.k`` (finding F3:
    calibration is defined at one candidate output per item).

    ``--offline``: a pure recompute from ``judgments.jsonl`` + the labels
    file (+ the overlay file, re-validated against ``judgments.jsonl``'s own
    persisted ``is_probe`` flags -- no fail-probe emails file needed
    offline). It makes zero calls of any kind, so it constructs no client at
    all and has no tracing requirement -- there is nothing here for Langfuse
    to ever observe.

    Both modes write ``data/calibration/certificate.json``, which every
    ``run``/``compare``/``gate`` report header consumes.
    """

    if offline:
        with _clean_exit_on_expected_errors():
            calib_labels = calibrate_module.load_calibration_labels(labels_path)
            judgments = calibrate_module.load_judgments_jsonl(judgments_path)
            overlay_rows = calibrate_module.load_perturbation_overlay(perturbations_path)
            resolved_date = (
                date.fromisoformat(date_override) if date_override is not None else None
            )
            label_file_hash = calibrate_module.hash_label_file(labels_path)

            result = calibrate_module.run_calibration_offline(
                judgments=judgments,
                labels=calib_labels,
                label_file_hash=label_file_hash,
                perturbation_overlay=overlay_rows,
                date_override=resolved_date,
            )
            certificate = calibrate_module.build_certificate(result)
            calibrate_module.write_certificate(certificate, DEFAULT_CERTIFICATE_PATH)
            report = calibrate_module.render_calibration_report(result)

        typer.echo(report)
        typer.echo(f"Certificate written to {DEFAULT_CERTIFICATE_PATH}")
        return

    _require_config_exists(config)
    with _clean_exit_on_expected_errors():
        cfg = load_config(config)
        effective_cfg, calib_items = _resolve_calibration_dataset(cfg, emails)
        # Finding F3: calibration is defined at ONE candidate output per item
        # (CalibrationLabel carries no replicate index, and build_triples
        # always takes the lowest-replicate row per item) -- force k=1 for
        # both candidate runs regardless of config.k, which defaults to 3 for
        # run/compare and would triple calibration spend for zero benefit.
        # Overridden via model_copy (the same technique _resolve_dataset uses
        # for its own config overrides) so _get_or_run's reuse-identity
        # computation and run_eval's own manifest agree on k=1 for every
        # calibration run -- config.k is never inherited here.
        calibrate_cfg = effective_cfg.model_copy(update={"k": 1})
        calib_labels = calibrate_module.load_calibration_labels(labels_path)
        overlay_rows = calibrate_module.load_perturbation_overlay(perturbations_path)

        # Finding: every DualAnnotationError/CalibrationBindingError this
        # command can raise over labels.jsonl is computable from the labels
        # alone (`resolve_gold_labels`) -- validate immediately, before the
        # TraceContext/client construction below, so a labels-file defect
        # costs zero API calls and no client setup (the same fail-before-
        # construction precedent as MissingTracingError). `run_calibration`
        # repeats this same pure resolution internally -- it must stay
        # correct standalone, so calling it twice here is fine.
        calibrate_module.resolve_gold_labels(calib_labels)

        # Fail-fast anchor (spec §5/§8, T9/T11): every TraceContext this
        # invocation could need -- the real calibration run's, AND the
        # fail-probe run's when a probe file exists -- is constructed before
        # ANY candidate or judge client (_get_or_run/_build_judge_client
        # below).
        trace_a = TraceContext.for_run(calibrate_cfg, True)
        trace_b = TraceContext.for_run(calibrate_cfg, True)

        has_probe_file = fail_probe_emails.exists()
        probe_cfg: Config | None = None
        probe_items: list[GoldenItem] | None = None
        trace_probe_a = trace_probe_b = None
        if has_probe_file:
            probe_cfg, probe_items = _resolve_calibration_dataset(calibrate_cfg, fail_probe_emails)
            trace_probe_a = TraceContext.for_run(probe_cfg, True)
            trace_probe_b = TraceContext.for_run(probe_cfg, True)

        run_dir_a = _get_or_run(
            calibrate_cfg, "a", calib_items, EXTRACTION_PROMPT, trace_a, DEFAULT_RUNS_ROOT
        )
        run_dir_b = _get_or_run(
            calibrate_cfg, "b", calib_items, EXTRACTION_PROMPT, trace_b, DEFAULT_RUNS_ROOT
        )
        run_a = load_run(run_dir_a)
        run_b = load_run(run_dir_b)

        probe_run_a = probe_run_b = None
        if has_probe_file:
            assert probe_cfg is not None and probe_items is not None
            probe_run_dir_a = _get_or_run(
                probe_cfg, "a", probe_items, EXTRACTION_PROMPT, trace_probe_a, DEFAULT_RUNS_ROOT
            )
            probe_run_dir_b = _get_or_run(
                probe_cfg, "b", probe_items, EXTRACTION_PROMPT, trace_probe_b, DEFAULT_RUNS_ROOT
            )
            probe_run_a = load_run(probe_run_dir_a)
            probe_run_b = load_run(probe_run_dir_b)

        judge = Judge(_build_judge_client(calibrate_cfg))
        resolved_date = date.fromisoformat(date_override) if date_override is not None else None
        label_file_hash = calibrate_module.hash_label_file(labels_path)

        result = calibrate_module.run_calibration(
            run_a=run_a,
            run_b=run_b,
            labels=calib_labels,
            judge=judge,
            label_file_hash=label_file_hash,
            probe_run_a=probe_run_a,
            probe_run_b=probe_run_b,
            perturbation_overlay=overlay_rows,
            date_override=resolved_date,
        )
        certificate = calibrate_module.build_certificate(result)
        calibrate_module.write_certificate(certificate, DEFAULT_CERTIFICATE_PATH)
        report = calibrate_module.render_calibration_report(result)

        # Finding F2: persist this live run's full judge output so a future
        # `eval calibrate --offline` can recompute the same report +
        # certificate with zero API calls / zero client construction.
        # `probe_item_ids` stamps each record's `is_probe` flag (fail-probe
        # design) so `--offline` can rederive probe-item membership without
        # ever needing a RunArtifact of its own.
        judgment_records = calibrate_module.judgment_records_from_judged(
            result.judged_triples,
            judge_version=result.judge_version,
            probe_item_ids=result.probe_item_ids,
        )
        calibrate_module.write_judgments_jsonl(
            judgments_path,
            judgments=judgment_records,
            self_consistency=result.self_consistency_records,
            judge_version=result.judge_version,
        )

    typer.echo(report)
    typer.echo(f"Certificate written to {DEFAULT_CERTIFICATE_PATH}")
    typer.echo(f"Judgments written to {judgments_path}")


@app.command()
def rescore(
    run_dir: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            help="Path to an existing run directory (manifest.json + rows.jsonl).",
        ),
    ],
) -> None:
    """Recompute a run's report from persisted raw outputs -- zero API calls, zero client build."""

    with _clean_exit_on_expected_errors():
        artifact = load_run(RunDir(path=run_dir))
        certificate = _load_certificate(DEFAULT_CERTIFICATE_PATH)
        report = render_run_report(artifact, certificate=certificate, reportable=False)

    typer.echo(report)


if __name__ == "__main__":
    app()
