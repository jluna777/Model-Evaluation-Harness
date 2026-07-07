"""Typer CLI: ``run`` / ``compare`` / ``rescore`` (spec §9, ticket T11).

This module is the composition root for Phase A: it is the ONLY place that
constructs real provider SDK clients (``AnthropicClient``/``OpenAIClient``/
``GeminiClient``, bundled into a ``runner.ModelKey`` -- see ``runner.py``'s
module docstring for why the runner itself never imports provider SDKs) and
the only place that decides whether a run is *reportable* (spec §8) from the
``--dataset`` flag. ``eval gate``/``eval calibrate`` are wired in T16/T14 --
this module intentionally stops at ``run``/``compare``/``rescore``.

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
from collections.abc import Callable
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import anthropic
import openai
import typer
from google import genai

from harness.config import Config, load_config
from harness.models import ModelClient
from harness.models.anthropic_client import AnthropicClient
from harness.models.gemini_client import GeminiClient
from harness.models.openai_client import OpenAIClient
from harness.prompts import EXTRACTION_PROMPT, PromptTemplate
from harness.reports import MissingCertificateError, render_compare_report, render_run_report
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

app = typer.Typer(help="Structured-extraction eval harness: run, compare, rescore.")

DEFAULT_CONFIG_PATH = Path("configs/default.yaml")
DEFAULT_CERTIFICATE_PATH = Path("data/calibration/certificate.json")


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


def _require_api_key(provider: str, *env_vars: str) -> None:
    """Fail fast with ``MissingApiKeyError`` unless at least one of
    ``env_vars`` (a provider's SDK-accepted alternatives -- e.g. Gemini's
    ``GEMINI_API_KEY``/``GOOGLE_API_KEY``, read from the installed
    ``google-genai`` package) is set in the environment."""

    if any(os.environ.get(var) for var in env_vars):
        return
    raise MissingApiKeyError(provider, env_vars[0])


def _construct_or_missing_key(
    provider: str, env_var: str, build: Callable[[], ModelClient]
) -> ModelClient:
    """Runs ``build`` (a provider SDK client construction) and re-raises ANY
    exception it raises as this same provider's ``MissingApiKeyError`` --
    a fallback wrap behind ``_require_api_key``'s primary env check, so a
    future SDK behavior change (a new exception type, a check
    ``_require_api_key`` doesn't anticipate) still can't reintroduce an
    unmapped traceback here. Expected to essentially never fire in practice
    once ``_require_api_key`` has already passed."""

    try:
        return build()
    except Exception as exc:
        raise MissingApiKeyError(provider, env_var) from exc


def _build_model_key(label: str, config: Config) -> ModelKey:
    """Constructs the real ``ModelKey`` for ``label`` -- the only place in
    this codebase that instantiates a provider SDK client for a candidate or
    judge role. See the module docstring for the two distinct test
    techniques this enables.

    Every required env var is checked FIRST -- both the candidate's and the
    judge's, ``_require_api_key`` -- before any SDK client is constructed for
    EITHER role. This is what turns ``openai.OpenAI()``/``genai.Client()``'s
    own unmapped construction-time exceptions (see ``MissingApiKeyError``)
    into a clean, predictable failure instead of a raced traceback:
    construction never starts at all until every key this call will need is
    confirmed present, so (for example) a present Anthropic key can never let
    Anthropic construction run ahead of a still-missing Gemini key. The
    construction calls themselves are additionally wrapped
    (``_construct_or_missing_key``) as a fallback in case SDK behavior ever
    changes in a way the eager checks don't anticipate.
    """

    if label == "a":
        _require_api_key("Anthropic", "ANTHROPIC_API_KEY")
    else:
        _require_api_key("OpenAI", "OPENAI_API_KEY")
    _require_api_key("Gemini", "GEMINI_API_KEY", "GOOGLE_API_KEY")

    if label == "a":
        candidate_client: ModelClient = _construct_or_missing_key(
            "Anthropic",
            "ANTHROPIC_API_KEY",
            lambda: AnthropicClient(config.models.candidate_a, anthropic.Anthropic()),
        )
    else:
        candidate_client = _construct_or_missing_key(
            "OpenAI",
            "OPENAI_API_KEY",
            lambda: OpenAIClient(config.models.candidate_b, openai.OpenAI()),
        )
    judge_client: ModelClient = _construct_or_missing_key(
        "Gemini", "GEMINI_API_KEY", lambda: GeminiClient(config.models.judge, genai.Client())
    )

    return ModelKey(label=label, candidate_client=candidate_client, judge_client=judge_client)


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


# --------------------------------------------------------------------------
# Expected-failure exit mapping (module docstring).
# --------------------------------------------------------------------------


@contextmanager
def _clean_exit_on_expected_errors():
    try:
        yield
    except (
        RunAborted,
        RunConfigMismatch,
        MissingTracingError,
        MissingCertificateError,
        MissingApiKeyError,
    ) as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc


# --------------------------------------------------------------------------
# Commands.
# --------------------------------------------------------------------------


DatasetOption = Annotated[
    Path | None,
    typer.Option(
        "--dataset",
        help="Dev-stage dataset path. Omit to use the config's golden set (reportable).",
    ),
]
ConfigOption = Annotated[
    Path,
    typer.Option("--config", help="Path to the run configuration YAML."),
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
def rescore(
    run_dir: Annotated[
        Path,
        typer.Argument(help="Path to an existing run directory (manifest.json + rows.jsonl)."),
    ],
) -> None:
    """Recompute a run's report from persisted raw outputs -- zero API calls, zero client build."""

    with _clean_exit_on_expected_errors():
        artifact = load_run(RunDir(path=run_dir))
        certificate = _load_certificate(DEFAULT_CERTIFICATE_PATH)
        report = render_run_report(artifact, certificate=certificate, reportable=False)

    typer.echo(report)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
