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
constructing anything, ``_existing_complete_run`` recomputes the exact
deterministic run-directory path ``run_eval`` (``runner.py``) would use for
this invocation's (label, items, k, prompt version, dataset version/path,
requested candidate/judge model ids) by calling ``runner._run_dir_path``
directly rather than re-implementing its hash, so the two can never drift.
If a manifest already exists there with ``completed: true``, it is loaded
directly and no client is ever constructed for that candidate; otherwise
``_build_model_key`` + ``run_eval`` run (and resume) as usual. This is what
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
``MissingTracingError``, and ``MissingCertificateError`` are the enumerated
"expected failure" set -- caught uniformly by
``_clean_exit_on_expected_errors`` and turned into a one-line ``stderr``
message plus exit code 1, never a traceback. Any other exception (a bad
``--config``/``--dataset`` path, a malformed dataset file, ...) is a genuine
usage bug and is left to propagate normally.
"""

import hashlib
import json
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import anthropic
import openai
import typer
from google import genai

import harness.runner as runner_module
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


def _build_model_key(label: str, config: Config) -> ModelKey:
    """Constructs the real ``ModelKey`` for ``label`` -- the only place in
    this codebase that instantiates a provider SDK client for a candidate or
    judge role. See the module docstring for the two distinct test
    techniques this enables."""

    if label == "a":
        candidate_client: ModelClient = AnthropicClient(
            config.models.candidate_a, anthropic.Anthropic()
        )
    else:
        candidate_client = OpenAIClient(config.models.candidate_b, openai.OpenAI())
    judge_client: ModelClient = GeminiClient(config.models.judge, genai.Client())
    return ModelKey(label=label, candidate_client=candidate_client, judge_client=judge_client)


def _existing_complete_run(
    config: Config,
    label: str,
    items: list[GoldenItem],
    k: int,
    prompt: PromptTemplate,
    runs_root: Path,
) -> RunDir | None:
    """The deterministic run directory for these inputs, iff it already
    holds a completed manifest -- ``None`` otherwise (missing, or present
    but incomplete). Computed with zero client construction (module
    docstring's run-identity-reuse note)."""

    candidate_model_id = (
        config.models.candidate_a if label == "a" else config.models.candidate_b
    )
    run_dir_path = runner_module._run_dir_path(
        runs_root,
        label,
        items,
        k,
        prompt.version,
        config.dataset.version,
        config.dataset.path,
        candidate_model_id,
        config.models.judge,
    )
    manifest_path = run_dir_path / "manifest.json"
    if not manifest_path.exists():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not manifest.get("completed"):
        return None
    return RunDir(path=run_dir_path)


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

    existing = _existing_complete_run(config, label, items, config.k, prompt, runs_root)
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
    except (RunAborted, RunConfigMismatch, MissingTracingError, MissingCertificateError) as exc:
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
        trace = TraceContext.for_run(effective_cfg, reportable)
        run_dir_a = _get_or_run(
            effective_cfg, "a", items, EXTRACTION_PROMPT, trace, DEFAULT_RUNS_ROOT
        )
        run_dir_b = _get_or_run(
            effective_cfg, "b", items, EXTRACTION_PROMPT, trace, DEFAULT_RUNS_ROOT
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
