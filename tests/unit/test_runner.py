from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import BaseModel

import harness.runner as runner_module
from harness.config import Config, load_config
from harness.judge.judge import JudgeVerdict
from harness.models import StructuredResult, Usage
from harness.models.retry import TransportExhausted
from harness.prompts import EXTRACTION_PROMPT, PromptTemplate
from harness.runner import (
    ModelKey,
    RunAborted,
    RunConfigMismatch,
    RunDir,
    _check_manifest_compatible,
    load_run,
    run_eval,
)
from harness.schema import EmailInput, GoldenExpected, GoldenItem, GoldenMeta

DEFAULT_CONFIG_PATH = Path(__file__).parents[2] / "configs" / "default.yaml"
FIXTURE_RUN_DIR = Path(__file__).parents[1] / "fixtures" / "runs" / "sample-run"


def _config() -> Config:
    return load_config(DEFAULT_CONFIG_PATH)


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
    from harness.schema import TicketExtraction

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
    recording so tests can assert exact call counts under real concurrency."""

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


def _model_key(
    candidate: FakeModelClient | None = None, judge: FakeModelClient | None = None
) -> ModelKey:
    return ModelKey(
        label="a",
        candidate_client=candidate or FakeModelClient(make_result=lambda *a: success_result()),
        judge_client=judge or FakeModelClient(make_result=lambda *a: judge_pass_result()),
    )


class TestCandidateFailureScoring:
    def test_schema_invalid_scores_all_seven_zero_no_judge_calls(self, tmp_path):
        items = [make_item("item-0")]
        candidate = FakeModelClient(
            make_result=lambda *a: StructuredResult(
                output=None,
                failure="schema_invalid",
                raw="not json",
                usage=Usage(input_tokens=10, output_tokens=0),
                served_model_version="candidate-v1",
            )
        )
        judge = FakeModelClient(make_result=lambda *a: judge_pass_result())
        model_key = _model_key(candidate, judge)

        run_dir = run_eval(
            _config(), model_key, k=1, dataset=items, prompt=EXTRACTION_PROMPT, runs_root=tmp_path
        )
        artifact = load_run(run_dir)

        row = artifact.rows[0]
        assert row.raw_output == "not json"
        assert len(row.field_scores) == 7
        assert all(v == 0 for v in row.field_scores.values())
        assert row.raw_judge == {"issue_summary": None, "requested_action": None}
        assert row.judge_rationales == {"issue_summary": None, "requested_action": None}
        assert judge.call_count == 0

    def test_refusal_scores_all_seven_zero_no_judge_calls(self, tmp_path):
        items = [make_item("item-0")]
        candidate = FakeModelClient(
            make_result=lambda *a: StructuredResult(
                output=None,
                failure="refusal",
                raw="finish_reason=SAFETY",
                usage=Usage(input_tokens=10, output_tokens=0),
                served_model_version="candidate-v1",
            )
        )
        judge = FakeModelClient(make_result=lambda *a: judge_pass_result())
        model_key = _model_key(candidate, judge)

        run_dir = run_eval(
            _config(), model_key, k=1, dataset=items, prompt=EXTRACTION_PROMPT, runs_root=tmp_path
        )
        artifact = load_run(run_dir)

        row = artifact.rows[0]
        assert row.raw_output == "finish_reason=SAFETY"
        assert all(v == 0 for v in row.field_scores.values())
        assert judge.call_count == 0


class TestJudgeErrorScoring:
    def test_judge_error_scores_missing_never_fail_and_is_counted(self, tmp_path):
        items = [make_item("item-0")]

        def judge_make_result(idx: int, prompt: str, schema: type) -> StructuredResult:
            if "requested_action" in prompt:
                return StructuredResult(
                    output=None,
                    failure="schema_invalid",
                    raw="not valid json",
                    usage=Usage(input_tokens=3, output_tokens=0),
                    served_model_version="judge-v1",
                )
            return judge_pass_result()

        judge = FakeModelClient(make_result=judge_make_result)
        model_key = _model_key(judge=judge)

        run_dir = run_eval(
            _config(), model_key, k=1, dataset=items, prompt=EXTRACTION_PROMPT, runs_root=tmp_path
        )
        artifact = load_run(run_dir)

        row = artifact.rows[0]
        assert row.field_scores["requested_action"] is None
        assert row.field_scores["requested_action"] != "fail"
        assert row.field_scores["requested_action"] != 0
        assert row.raw_judge["requested_action"] == "not valid json"
        assert row.judge_rationales["requested_action"] is None
        assert row.field_scores["issue_summary"] == 1
        assert artifact.judge_error_count() == 1


class TestAbortOnTransportExhausted:
    def test_abort_is_distinct_from_completed_run_and_keeps_partial_file(self, tmp_path):
        items = [make_item(f"item-{i}") for i in range(5)]

        def make_result(idx: int, prompt: str, schema: type) -> StructuredResult:
            if idx == 3:
                raise TransportExhausted(4, RuntimeError("rate limited"))
            return success_result()

        candidate = FakeModelClient(make_result=make_result)
        model_key = _model_key(candidate)

        with pytest.raises(RunAborted) as exc_info:
            run_eval(
                _config(),
                model_key,
                k=1,
                dataset=items,
                prompt=EXTRACTION_PROMPT,
                runs_root=tmp_path,
                max_workers=1,
            )

        aborted = exc_info.value
        assert isinstance(aborted.cause, TransportExhausted)

        rows_path = aborted.run_dir.path / "rows.jsonl"
        assert rows_path.exists()
        lines = rows_path.read_text(encoding="utf-8").splitlines()
        persisted = [json.loads(line) for line in lines]
        assert len(persisted) == 3
        assert {row["item_id"] for row in persisted} == {"item-0", "item-1", "item-2"}

        artifact = load_run(aborted.run_dir)
        assert artifact.completed is False
        assert len(artifact.rows) == 3


class TestResume:
    def test_resume_skips_completed_rows_no_respend(self, tmp_path):
        items = [make_item(f"item-{i}") for i in range(5)]

        def make_result(idx: int, prompt: str, schema: type) -> StructuredResult:
            if idx == 3:
                raise TransportExhausted(4, RuntimeError("boom"))
            return success_result()

        candidate = FakeModelClient(make_result=make_result)
        model_key = _model_key(candidate)
        config = _config()

        with pytest.raises(RunAborted) as exc_info:
            run_eval(
                config,
                model_key,
                k=1,
                dataset=items,
                prompt=EXTRACTION_PROMPT,
                runs_root=tmp_path,
                max_workers=1,
            )

        run_dir = exc_info.value.run_dir
        partial = load_run(run_dir)
        assert {row.item_id for row in partial.rows} == {"item-0", "item-1", "item-2"}

        result_dir = run_eval(
            config,
            model_key,
            k=1,
            dataset=items,
            prompt=EXTRACTION_PROMPT,
            runs_root=tmp_path,
            max_workers=1,
        )
        assert result_dir.path == run_dir.path

        artifact = load_run(result_dir)
        assert artifact.completed is True
        assert {row.item_id for row in artifact.rows} == {f"item-{i}" for i in range(5)}
        assert len(artifact.rows) == 5

        # 6 total candidate calls: items 0,1,2 succeed once each (3), item 3's
        # first attempt transport-fails (1), then on resume item 3 succeeds and
        # item 4 succeeds (2) -- items 0,1,2 are never called a second time.
        assert candidate.call_count == 6


class TestIncrementalWrites:
    def test_rows_exist_on_disk_before_the_run_finishes(self, tmp_path):
        items = [make_item(f"item-{i}") for i in range(4)]
        config = _config()
        reached = threading.Event()
        release = threading.Event()

        def make_result(idx: int, prompt: str, schema: type) -> StructuredResult:
            if idx == 3:
                reached.set()
                assert release.wait(timeout=5), "test deadlocked waiting for release"
            return success_result()

        candidate = FakeModelClient(make_result=make_result)
        model_key = _model_key(candidate)
        holder: dict[str, RunDir] = {}

        def _run() -> None:
            holder["run_dir"] = run_eval(
                config,
                model_key,
                k=1,
                dataset=items,
                prompt=EXTRACTION_PROMPT,
                runs_root=tmp_path,
                max_workers=1,
            )

        thread = threading.Thread(target=_run)
        thread.start()
        try:
            assert reached.wait(timeout=5), "4th candidate call never started"

            run_dir_path = runner_module._run_dir_path(
                Path(tmp_path),
                "a",
                items,
                1,
                EXTRACTION_PROMPT.version,
                config.dataset.version,
                config.dataset.path,
            )
            rows_path = run_dir_path / "rows.jsonl"
            persisted = [
                json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines()
            ]
            assert len(persisted) == 3
            assert {row["item_id"] for row in persisted} == {"item-0", "item-1", "item-2"}
        finally:
            release.set()
            thread.join(timeout=5)
        assert not thread.is_alive()

        artifact = load_run(holder["run_dir"])
        assert artifact.completed is True
        assert len(artifact.rows) == 4


class TestFingerprint:
    def test_prompt_version_change_changes_fingerprint(self, tmp_path):
        items = [make_item("item-0")]
        config = _config()

        def run_with_prompt_version(version: int):
            model_key = _model_key()
            prompt = PromptTemplate(version=version, template=EXTRACTION_PROMPT.template)
            run_dir = run_eval(
                config, model_key, k=1, dataset=items, prompt=prompt, runs_root=tmp_path
            )
            return load_run(run_dir)

        artifact_v1 = run_with_prompt_version(1)
        artifact_v2 = run_with_prompt_version(2)

        assert artifact_v1.prompt_version == 1
        assert artifact_v2.prompt_version == 2
        assert artifact_v1.fingerprint != artifact_v2.fingerprint


class TestLoadRunRoundTrip:
    def test_fixture_run_dir_round_trips(self):
        run_dir = RunDir(path=FIXTURE_RUN_DIR)

        artifact = load_run(run_dir)

        assert artifact.completed is True
        assert artifact.model_key == "a"
        assert artifact.k == 1
        assert len(artifact.items) == 2
        assert len(artifact.rows) == 2

        persisted_rows = [
            json.loads(line)
            for line in (FIXTURE_RUN_DIR / "rows.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        for row, persisted in zip(artifact.rows, persisted_rows, strict=True):
            assert row.item_id == persisted["item_id"]
            assert row.replicate == persisted["replicate"]
            assert row.raw_output == persisted["raw_output"]
            assert row.field_scores == persisted["field_scores"]
            assert row.raw_judge == persisted["raw_judge"]
            assert row.judge_rationales == persisted["judge_rationales"]
            assert row.usage == persisted["usage"]
            assert row.served_model_version == persisted["served_model_version"]

        assert artifact.slice_for("fixture-1") == "nominal"
        assert artifact.slice_for("fixture-2") == "adversarial"
        assert artifact.judge_error_count() == 1
        assert artifact.usage_totals() == {"input_tokens": 230, "output_tokens": 75}

    def test_dynamic_run_round_trips_rows_match_jsonl(self, tmp_path):
        items = [make_item("item-0"), make_item("item-1", slice_="adversarial")]
        model_key = _model_key()

        run_dir = run_eval(
            _config(), model_key, k=2, dataset=items, prompt=EXTRACTION_PROMPT, runs_root=tmp_path
        )
        artifact = load_run(run_dir)

        persisted_rows = [
            json.loads(line)
            for line in (run_dir.path / "rows.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert len(artifact.rows) == len(persisted_rows) == 4
        persisted_keys = {(r["item_id"], r["replicate"]) for r in persisted_rows}
        artifact_keys = {(row.item_id, row.replicate) for row in artifact.rows}
        assert persisted_keys == artifact_keys


class TestBoundedConcurrency:
    def test_default_concurrency_completes_all_rows_without_corruption(self, tmp_path):
        items = [make_item(f"item-{i}") for i in range(10)]
        candidate = FakeModelClient(make_result=lambda *a: success_result())
        judge = FakeModelClient(make_result=lambda *a: judge_pass_result())
        model_key = _model_key(candidate, judge)

        run_dir = run_eval(
            _config(), model_key, k=3, dataset=items, prompt=EXTRACTION_PROMPT, runs_root=tmp_path
        )
        artifact = load_run(run_dir)

        assert artifact.completed is True
        assert len(artifact.rows) == 30
        assert candidate.call_count == 30
        assert judge.call_count == 60  # 2 judged fields x 30 rows

        keys = [(row.item_id, row.replicate) for row in artifact.rows]
        assert len(keys) == len(set(keys)) == 30


class TestManifestCompatibilityCheck:
    def test_raises_on_k_mismatch(self, tmp_path):
        manifest = {
            "model_key": "a",
            "k": 3,
            "prompt_version": 1,
            "dataset_version": 1,
            "item_ids": ["item-0"],
        }
        model_key = _model_key()

        with pytest.raises(RunConfigMismatch) as exc_info:
            _check_manifest_compatible(
                manifest,
                tmp_path,
                model_key,
                [make_item("item-0")],
                5,
                EXTRACTION_PROMPT,
                _config(),
            )

        assert "k" in exc_info.value.mismatches
