"""Tests for the Typer CLI (T11): ``run``, ``compare``, ``rescore``.

Uses ``typer.testing.CliRunner`` throughout. Every test that needs a working
candidate/judge monkeypatches ``harness.cli._build_model_key`` (the factory
seam -- see ``cli.py``'s module docstring) with hand-written fakes; no real
provider SDK client is ever constructed by this test module. Tests proving
*zero* client construction instead monkeypatch the three concrete classes
(``AnthropicClient``/``OpenAIClient``/``GeminiClient``) ``cli.py`` imports by
name to a stub whose ``__init__`` raises -- a stronger proof than spying on
the seam function alone, since it would also catch a future bug that
constructs one of these classes through some other code path.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel
from typer.testing import CliRunner

import harness.cli as cli
import harness.runner as runner_module
from harness.cli import app
from harness.config import load_config
from harness.judge.judge import JudgeVerdict
from harness.models import StructuredResult, Usage
from harness.models.retry import TransportExhausted
from harness.prompts import EXTRACTION_PROMPT
from harness.reports import render_run_report
from harness.runner import DEFAULT_RUNS_ROOT, ModelKey, RunDir, load_run, run_eval
from harness.schema import EmailInput, GoldenExpected, GoldenItem, GoldenMeta, TicketExtraction

DEFAULT_CONFIG_PATH = Path(__file__).parents[2] / "configs" / "default.yaml"
CERT_FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "reports" / "certificate_adequate.json"
)
RUN_REPORT_FIXTURE_DIR = (
    Path(__file__).parents[1] / "fixtures" / "reports" / "run_report" / "run"
)

runner = CliRunner()


# --------------------------------------------------------------------------
# Shared fixtures/helpers (mirrors tests/unit/test_runner.py's conventions).
# --------------------------------------------------------------------------


def make_item(item_id: str, *, slice_: str = "nominal") -> GoldenItem:
    email_kwargs = {"from": f"{item_id}@example.com", "subject": "Subject", "body": "Body text."}
    return GoldenItem(
        id=item_id,
        email=EmailInput(**email_kwargs),
        expected=GoldenExpected(
            category="billing",
            priority="normal",
            customer_name="Jane Doe",
            order_id=None,
            product_name=None,
            issue_summary="Customer's order arrived damaged.",
            requested_action="Customer wants a refund.",
        ),
        meta=GoldenMeta(
            slice=slice_,
            categories=["billing"],
            difficulty=1,
            generator="gpt-4",
            edited=False,
            notes="",
        ),
    )


def success_result(served_model_version: str = "candidate-v1") -> StructuredResult:
    output = TicketExtraction(
        category="billing",
        priority="normal",
        customer_name="Jane Doe",
        order_id=None,
        product_name=None,
        issue_summary="Customer's order arrived damaged.",
        requested_action="Customer wants a refund.",
    )
    return StructuredResult(
        output=output,
        failure=None,
        raw=output.model_dump_json(),
        usage=Usage(input_tokens=50, output_tokens=20),
        served_model_version=served_model_version,
    )


def judge_pass_result() -> StructuredResult:
    output = JudgeVerdict(verdict="pass", rationale="Same issue and action.")
    return StructuredResult(
        output=output,
        failure=None,
        raw=output.model_dump_json(),
        usage=Usage(input_tokens=5, output_tokens=5),
        served_model_version="judge-v1",
    )


@dataclass
class FakeModelClient:
    """Test double for the ``ModelClient`` protocol -- thread-safe call
    recording, mirroring ``tests/unit/test_runner.py``'s own fake."""

    make_result: Callable[[int, str, type[BaseModel]], StructuredResult]
    calls: list[tuple[str, type]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def complete_structured(self, prompt: str, schema: type[BaseModel]) -> StructuredResult:
        with self._lock:
            idx = len(self.calls)
            self.calls.append((prompt, schema))
        return self.make_result(idx, prompt, schema)

    @property
    def call_count(self) -> int:
        return len(self.calls)


def _fake_build_model_key_factory(
    registry: dict[str, list[FakeModelClient]],
) -> Callable[[str, object], ModelKey]:
    """A ``_build_model_key``-shaped fake that records every constructed
    fake candidate/judge client under ``registry`` (keyed "candidate"/
    "judge") -- so a test can assert on call counts/instances afterward."""

    def factory(label: str, config: object) -> ModelKey:
        candidate = FakeModelClient(make_result=lambda *a: success_result())
        judge = FakeModelClient(make_result=lambda *a: judge_pass_result())
        registry.setdefault("candidate", []).append(candidate)
        registry.setdefault("judge", []).append(judge)
        return ModelKey(label=label, candidate_client=candidate, judge_client=judge)

    return factory


class _RaisingClient:
    """Stand-in for AnthropicClient/OpenAIClient/GeminiClient that raises on
    construction -- proves the real construction path was never reached."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise AssertionError(f"{self.__class__.__name__} must not be constructed")


def _forbid_real_client_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "AnthropicClient", _RaisingClient)
    monkeypatch.setattr(cli, "OpenAIClient", _RaisingClient)
    monkeypatch.setattr(cli, "GeminiClient", _RaisingClient)


def _write_dataset(path: Path, items: list[GoldenItem]) -> Path:
    lines = [json.dumps(item.model_dump(mode="json")) for item in items]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_config(
    path: Path, *, dataset_path: Path, dataset_version: int = 1, k: int = 1
) -> Path:
    base = load_config(DEFAULT_CONFIG_PATH)
    updated = base.model_copy(
        update={
            "dataset": base.dataset.model_copy(
                update={"path": str(dataset_path), "version": dataset_version}
            ),
            "k": k,
        }
    )
    path.write_text(yaml.safe_dump(updated.model_dump(mode="json")), encoding="utf-8")
    return path


class _FakeTraceContext:
    """Duck-typed stand-in for ``TraceContext`` -- always "traced", never
    touches Langfuse. Used only where a test needs ``reportable=True`` to
    survive past the tracing step without real credentials."""

    untraced = False

    @staticmethod
    def for_run(config: object, reportable: bool, **kwargs: object) -> _FakeTraceContext:
        return _FakeTraceContext()

    def candidate_span(self, **kwargs: object):
        return contextlib.nullcontext()

    def judge_span(self, **kwargs: object):
        return contextlib.nullcontext()

    def record_item_scores(self, **kwargs: object) -> None:
        pass

    def flush(self) -> None:
        pass


# --------------------------------------------------------------------------
# `eval --help`
# --------------------------------------------------------------------------


class TestHelp:
    def test_help_exits_zero_and_lists_all_three_commands(self):
        result = runner.invoke(app, ["--help"])

        assert result.exit_code == 0
        assert "run" in result.output
        assert "compare" in result.output
        assert "rescore" in result.output


# --------------------------------------------------------------------------
# `eval rescore`
# --------------------------------------------------------------------------


class TestRescore:
    def test_rescore_matches_a_direct_render_call_with_certificate(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cert_dir = tmp_path / "data" / "calibration"
        cert_dir.mkdir(parents=True)
        (cert_dir / "certificate.json").write_text(
            CERT_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
        )

        artifact = load_run(RunDir(path=RUN_REPORT_FIXTURE_DIR))
        certificate = cli._load_certificate(Path("data/calibration/certificate.json"))
        expected = render_run_report(artifact, certificate=certificate, reportable=False)

        result = runner.invoke(app, ["rescore", str(RUN_REPORT_FIXTURE_DIR)])

        assert result.exit_code == 0, result.output
        assert result.output.rstrip("\n") == expected.rstrip("\n")

    def test_rescore_is_byte_identical_across_repeated_invocations(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        first = runner.invoke(app, ["rescore", str(RUN_REPORT_FIXTURE_DIR)])
        second = runner.invoke(app, ["rescore", str(RUN_REPORT_FIXTURE_DIR)])

        assert first.exit_code == second.exit_code == 0
        assert first.output == second.output

    def test_rescore_never_constructs_a_client(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _forbid_real_client_construction(monkeypatch)

        result = runner.invoke(app, ["rescore", str(RUN_REPORT_FIXTURE_DIR)])

        assert result.exit_code == 0, result.output


# --------------------------------------------------------------------------
# `eval run --dataset ...` (required anchor: non-reportable dev run)
# --------------------------------------------------------------------------


class TestRunDevDataset:
    def test_dataset_run_is_untraced_non_reportable_with_dev_dataset_version(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

        dataset_path = _write_dataset(tmp_path / "dev.jsonl", [make_item("item-0")])
        config_path = _write_config(
            tmp_path / "config.yaml", dataset_path=Path("data/golden/golden.jsonl")
        )
        golden_default_version = load_config(config_path).dataset.version

        registry: dict[str, list[FakeModelClient]] = {}
        monkeypatch.setattr(cli, "_build_model_key", _fake_build_model_key_factory(registry))

        with pytest.warns(UserWarning, match="No Langfuse credentials"):
            result = runner.invoke(
                app,
                [
                    "run",
                    "--model",
                    "a",
                    "--dataset",
                    str(dataset_path),
                    "--config",
                    str(config_path),
                ],
            )

        assert result.exit_code == 0, result.output

        run_dirs = list((tmp_path / "results" / "runs").glob("a-*"))
        assert len(run_dirs) == 1
        artifact = load_run(RunDir(path=run_dirs[0]))

        assert artifact.untraced is True
        assert artifact.dataset_version == cli._dev_dataset_version(dataset_path)
        assert artifact.dataset_version != golden_default_version

        report_text = (run_dirs[0] / "report.md").read_text(encoding="utf-8")
        assert "UNCALIBRATED" in report_text
        assert "UNTRACED" in report_text


# --------------------------------------------------------------------------
# `eval compare`
# --------------------------------------------------------------------------


class TestCompareReuse:
    def test_compare_reuses_matching_runs_without_constructing_any_client(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        items = [make_item("item-0"), make_item("item-1", slice_="adversarial")]
        dataset_path = _write_dataset(tmp_path / "dev.jsonl", items)
        config_path = _write_config(
            tmp_path / "config.yaml", dataset_path=Path("data/golden/golden.jsonl")
        )
        effective_cfg, loaded_items, _ = cli._resolve_dataset(
            load_config(config_path), dataset_path
        )

        # Setup: pre-populate BOTH candidates' runs completely, using fakes
        # driven directly through run_eval -- not through the CLI. This is
        # setup, not the behavior under test.
        judge = FakeModelClient(make_result=lambda *a: judge_pass_result())
        candidate_a = FakeModelClient(make_result=lambda *a: success_result("candidate-a-v1"))
        candidate_b = FakeModelClient(make_result=lambda *a: success_result("candidate-b-v1"))
        run_eval(
            effective_cfg,
            ModelKey(label="a", candidate_client=candidate_a, judge_client=judge),
            k=effective_cfg.k,
            dataset=loaded_items,
            prompt=EXTRACTION_PROMPT,
            runs_root=DEFAULT_RUNS_ROOT,
        )
        run_eval(
            effective_cfg,
            ModelKey(label="b", candidate_client=candidate_b, judge_client=judge),
            k=effective_cfg.k,
            dataset=loaded_items,
            prompt=EXTRACTION_PROMPT,
            runs_root=DEFAULT_RUNS_ROOT,
        )
        calls_before = candidate_a.call_count, candidate_b.call_count

        # Behavior under test: `compare` must reuse both runs untouched.
        _forbid_real_client_construction(monkeypatch)

        def _forbid_factory(*args: object, **kwargs: object) -> ModelKey:
            raise AssertionError("_build_model_key must not be called when reusing matching runs")

        monkeypatch.setattr(cli, "_build_model_key", _forbid_factory)

        result = runner.invoke(
            app, ["compare", "--dataset", str(dataset_path), "--config", str(config_path)]
        )

        assert result.exit_code == 0, result.output
        assert (candidate_a.call_count, candidate_b.call_count) == calls_before
        assert "Compare Report" in result.output

    def test_compare_reruns_when_no_matching_run_exists(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        items = [make_item("item-0")]
        dataset_path = _write_dataset(tmp_path / "dev.jsonl", items)
        config_path = _write_config(
            tmp_path / "config.yaml", dataset_path=Path("data/golden/golden.jsonl")
        )

        registry: dict[str, list[FakeModelClient]] = {}
        monkeypatch.setattr(cli, "_build_model_key", _fake_build_model_key_factory(registry))

        result = runner.invoke(
            app, ["compare", "--dataset", str(dataset_path), "--config", str(config_path)]
        )

        assert result.exit_code == 0, result.output
        assert len(registry["candidate"]) == 2  # one fresh ModelKey per candidate
        assert registry["candidate"][0].call_count > 0
        assert registry["candidate"][1].call_count > 0
        assert "Compare Report" in result.output


# --------------------------------------------------------------------------
# Expected-failure clean exits.
# --------------------------------------------------------------------------


class TestExpectedFailureExits:
    def test_missing_tracing_error_on_golden_run_without_keys(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

        dataset_path = _write_dataset(tmp_path / "golden.jsonl", [make_item("item-0")])
        config_path = _write_config(tmp_path / "config.yaml", dataset_path=dataset_path)

        result = runner.invoke(app, ["run", "--model", "a", "--config", str(config_path)])

        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "credentials" in result.output.lower() or "langfuse" in result.output.lower()

    def test_missing_certificate_error_on_reportable_run(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        dataset_path = _write_dataset(tmp_path / "golden.jsonl", [make_item("item-0")])
        config_path = _write_config(tmp_path / "config.yaml", dataset_path=dataset_path)

        registry: dict[str, list[FakeModelClient]] = {}
        monkeypatch.setattr(cli, "_build_model_key", _fake_build_model_key_factory(registry))
        monkeypatch.setattr(cli, "TraceContext", _FakeTraceContext)

        result = runner.invoke(app, ["run", "--model", "a", "--config", str(config_path)])

        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "certificate" in result.output.lower()

    def test_run_config_mismatch_clean_exit(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        items = [make_item("item-0")]
        dataset_path = _write_dataset(tmp_path / "dev.jsonl", items)
        config_path = _write_config(
            tmp_path / "config.yaml", dataset_path=Path("data/golden/golden.jsonl")
        )
        effective_cfg, loaded_items, _ = cli._resolve_dataset(
            load_config(config_path), dataset_path
        )

        run_dir_path = runner_module._run_dir_path(
            DEFAULT_RUNS_ROOT,
            "a",
            loaded_items,
            effective_cfg.k,
            EXTRACTION_PROMPT.version,
            effective_cfg.dataset.version,
            effective_cfg.dataset.path,
            effective_cfg.models.candidate_a,
            effective_cfg.models.judge,
        )
        run_dir_path.mkdir(parents=True)
        manifest = {
            "model_key": "a",
            "candidate_model_id": effective_cfg.models.candidate_a,
            "judge_model_id": effective_cfg.models.judge,
            "k": 999,  # deliberately mismatched vs effective_cfg.k
            "prompt_version": EXTRACTION_PROMPT.version,
            "dataset_path": effective_cfg.dataset.path,
            "dataset_version": effective_cfg.dataset.version,
            "item_ids": [item.id for item in loaded_items],
            "items": [item.model_dump(mode="json") for item in loaded_items],
            "served_versions": {},
            "judge_version": "irrelevant-fixture-value",
            "composite_mode": "FULL_7",
            "calibration_verdict": "uncalibrated",
            "fingerprint": None,
            "completed": False,
            "untraced": True,
            "created_at": "2026-07-04T00:00:00+00:00",
        }
        (run_dir_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        registry: dict[str, list[FakeModelClient]] = {}
        monkeypatch.setattr(cli, "_build_model_key", _fake_build_model_key_factory(registry))

        result = runner.invoke(
            app,
            ["run", "--model", "a", "--dataset", str(dataset_path), "--config", str(config_path)],
        )

        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "k" in result.output

    def test_run_aborted_clean_exit(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        items = [make_item("item-0"), make_item("item-1")]
        dataset_path = _write_dataset(tmp_path / "dev.jsonl", items)
        config_path = _write_config(
            tmp_path / "config.yaml", dataset_path=Path("data/golden/golden.jsonl")
        )

        def factory(label: str, config: object) -> ModelKey:
            def make_result(idx: int, prompt: str, schema: type) -> StructuredResult:
                raise TransportExhausted(4, RuntimeError("boom"))

            candidate = FakeModelClient(make_result=make_result)
            judge = FakeModelClient(make_result=lambda *a: judge_pass_result())
            return ModelKey(label=label, candidate_client=candidate, judge_client=judge)

        monkeypatch.setattr(cli, "_build_model_key", factory)

        result = runner.invoke(
            app,
            ["run", "--model", "a", "--dataset", str(dataset_path), "--config", str(config_path)],
        )

        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "aborted" in result.output.lower()
