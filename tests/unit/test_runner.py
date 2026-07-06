from __future__ import annotations

import json
import threading
import time
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


class TestAbortOnAnyWorkerException:
    """C2: a non-``TransportExhausted`` worker exception must still produce
    the ONE distinct ``RunAborted`` outcome, and must never leave the thread
    pool orphaned -- spending after ``run_eval`` has already returned
    control to the caller."""

    def test_non_transport_exception_aborts_without_orphaning_pool(self, tmp_path):
        items = [make_item(f"item-{i}") for i in range(8)]

        def make_result(idx: int, prompt: str, schema: type) -> StructuredResult:
            if idx == 2:  # the 3rd call
                raise ValueError("boom")
            time.sleep(0.05)
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
                max_workers=4,
            )

        aborted = exc_info.value
        assert isinstance(aborted.cause, ValueError)
        assert isinstance(aborted.__cause__, ValueError)

        # The pool must already be fully shut down by the time run_eval
        # raises -- not still running tasks in the background. A short sleep
        # after control returns must show zero additional spend.
        stable_count = candidate.call_count
        time.sleep(0.3)
        assert candidate.call_count == stable_count, (
            "worker pool kept spending after run_eval returned control"
        )

        rows_path = aborted.run_dir.path / "rows.jsonl"
        assert rows_path.exists()
        persisted = [
            json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines()
        ]
        # Partial rows from tasks already in flight when the failure was
        # observed stay intact; not-yet-started tasks never ran at all.
        assert 0 < len(persisted) < 8

        artifact = load_run(aborted.run_dir)
        assert artifact.completed is False


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
                config.models.candidate_a,
                config.models.judge,
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


class TestModelIdentityInRunIdentity:
    """C1: run identity must key on the *requested* model id, not just the
    abstract "a"/"b" label -- otherwise swapping the configured candidate or
    judge model under an unchanged label/config-shape silently resumes a
    stale run's rows (zero new calls, completed=True) for what is actually a
    different model."""

    def test_changed_candidate_model_id_does_not_resume_stale_run(self, tmp_path):
        items = [make_item("item-0")]
        config_v1 = _config()
        judge = FakeModelClient(make_result=lambda *a: judge_pass_result())
        candidate_v1 = FakeModelClient(make_result=lambda *a: success_result())
        model_key_v1 = ModelKey(label="a", candidate_client=candidate_v1, judge_client=judge)

        run_dir_v1 = run_eval(
            config_v1,
            model_key_v1,
            k=1,
            dataset=items,
            prompt=EXTRACTION_PROMPT,
            runs_root=tmp_path,
        )
        artifact_v1 = load_run(run_dir_v1)
        assert candidate_v1.call_count == 1
        assert artifact_v1.completed is True

        # Same label, same k/dataset/prompt -- only the *configured*
        # candidate model id changed (a served-version swap of the same
        # candidate slot). This must NOT be treated as the same run.
        config_v2 = config_v1.model_copy(
            update={
                "models": config_v1.models.model_copy(
                    update={"candidate_a": "some-other-candidate-model-version"}
                )
            }
        )
        candidate_v2 = FakeModelClient(make_result=lambda *a: success_result())
        model_key_v2 = ModelKey(label="a", candidate_client=candidate_v2, judge_client=judge)

        run_dir_v2 = run_eval(
            config_v2,
            model_key_v2,
            k=1,
            dataset=items,
            prompt=EXTRACTION_PROMPT,
            runs_root=tmp_path,
        )

        assert run_dir_v2.path != run_dir_v1.path
        # A fresh call was actually made -- not resumed from candidate_v1's
        # stale rows.
        assert candidate_v2.call_count == 1
        assert candidate_v1.call_count == 1

    def test_changed_judge_model_id_does_not_resume_stale_run(self, tmp_path):
        items = [make_item("item-0")]
        config_v1 = _config()
        model_key_v1 = _model_key()

        run_dir_v1 = run_eval(
            config_v1,
            model_key_v1,
            k=1,
            dataset=items,
            prompt=EXTRACTION_PROMPT,
            runs_root=tmp_path,
        )

        config_v2 = config_v1.model_copy(
            update={
                "models": config_v1.models.model_copy(
                    update={"judge": "some-other-judge-model-version"}
                )
            }
        )
        model_key_v2 = _model_key()

        run_dir_v2 = run_eval(
            config_v2,
            model_key_v2,
            k=1,
            dataset=items,
            prompt=EXTRACTION_PROMPT,
            runs_root=tmp_path,
        )

        assert run_dir_v2.path != run_dir_v1.path


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

    def test_raises_on_candidate_model_id_mismatch(self, tmp_path):
        """C1: a manifest recorded against a different candidate model id
        than the current call's config must be flagged incompatible, even
        when the label/k/prompt/dataset all agree (the residual case a hash
        collision or a hand-edited manifest could otherwise slip through)."""

        config = _config()
        manifest = {
            "model_key": "a",
            "candidate_model_id": "stale-candidate-model-id",
            "judge_model_id": config.models.judge,
            "k": 1,
            "prompt_version": EXTRACTION_PROMPT.version,
            "dataset_version": config.dataset.version,
            "item_ids": ["item-0"],
        }
        model_key = _model_key()

        with pytest.raises(RunConfigMismatch) as exc_info:
            _check_manifest_compatible(
                manifest, tmp_path, model_key, [make_item("item-0")], 1, EXTRACTION_PROMPT, config
            )

        assert "candidate_model_id" in exc_info.value.mismatches

    def test_raises_on_judge_model_id_mismatch(self, tmp_path):
        config = _config()
        manifest = {
            "model_key": "a",
            "candidate_model_id": config.models.candidate_a,
            "judge_model_id": "stale-judge-model-id",
            "k": 1,
            "prompt_version": EXTRACTION_PROMPT.version,
            "dataset_version": config.dataset.version,
            "item_ids": ["item-0"],
        }
        model_key = _model_key()

        with pytest.raises(RunConfigMismatch) as exc_info:
            _check_manifest_compatible(
                manifest, tmp_path, model_key, [make_item("item-0")], 1, EXTRACTION_PROMPT, config
            )

        assert "judge_model_id" in exc_info.value.mismatches


class TestJudgeUsageAccounting:
    """I3: judge usage and served model version must be captured, not
    structurally discarded -- run rows carry per-field judge usage, and the
    manifest's served_versions records the judge's runtime-observed
    identity alongside the candidate's."""

    def test_rows_carry_judge_usage_and_manifest_has_judge_served_version(self, tmp_path):
        items = [make_item("item-0")]
        candidate = FakeModelClient(make_result=lambda *a: success_result())
        judge = FakeModelClient(make_result=lambda *a: judge_pass_result())
        model_key = _model_key(candidate, judge)

        run_dir = run_eval(
            _config(), model_key, k=1, dataset=items, prompt=EXTRACTION_PROMPT, runs_root=tmp_path
        )
        artifact = load_run(run_dir)

        row = artifact.rows[0]
        assert set(row.judge_usage) == {"issue_summary", "requested_action"}
        for field_usage in row.judge_usage.values():
            assert field_usage == {"input_tokens": 5, "output_tokens": 5}

        manifest = json.loads((run_dir.path / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["served_versions"]["judge"] == "judge-v1"
        assert manifest["served_versions"]["candidate_a"] == "candidate-v1"

        assert artifact.judge_usage_totals() == {"input_tokens": 10, "output_tokens": 10}
        # Candidate-only totals stay exactly as before -- unaffected by the
        # additive judge usage tracking.
        assert artifact.usage_totals() == {"input_tokens": 50, "output_tokens": 20}

    def test_candidate_failure_row_has_no_judge_usage_per_field(self, tmp_path):
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
        model_key = _model_key(candidate)

        run_dir = run_eval(
            _config(), model_key, k=1, dataset=items, prompt=EXTRACTION_PROMPT, runs_root=tmp_path
        )
        artifact = load_run(run_dir)

        row = artifact.rows[0]
        assert row.judge_usage == {"issue_summary": None, "requested_action": None}

        manifest = json.loads((run_dir.path / "manifest.json").read_text(encoding="utf-8"))
        # No judge call was ever made, so no judge served version to report.
        assert "judge" not in manifest["served_versions"]

    def test_fixture_rows_without_judge_usage_contribute_nothing(self):
        """Rows persisted before I3 (no ``judge_usage`` key at all) must
        still load and total to zero judge usage, not raise."""

        artifact = load_run(RunDir(path=FIXTURE_RUN_DIR))

        assert all(row.judge_usage is None for row in artifact.rows)
        assert artifact.judge_usage_totals() == {"input_tokens": 0, "output_tokens": 0}


class TestTruncatedTrailingLine:
    """I4: a crash mid-write can leave ``rows.jsonl``'s final line as an
    unparseable JSON fragment. That must be forgiven (dropped with a
    warning, re-executed on resume) -- but only when it's the LAST line;
    unparseable content anywhere else in the file is real corruption and
    must still raise."""

    def test_truncated_last_line_is_dropped_with_warning_and_rerun(self, tmp_path):
        items = [make_item("item-0"), make_item("item-1")]
        candidate = FakeModelClient(make_result=lambda *a: success_result())
        model_key = _model_key(candidate)
        config = _config()

        run_dir = run_eval(
            config, model_key, k=1, dataset=items, prompt=EXTRACTION_PROMPT, runs_root=tmp_path
        )
        assert candidate.call_count == 2

        rows_path = run_dir.path / "rows.jsonl"
        lines = rows_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        truncated = lines[-1][: len(lines[-1]) // 2]
        rows_path.write_text(lines[0] + "\n" + truncated, encoding="utf-8")

        with pytest.warns(UserWarning):
            result_dir = run_eval(
                config, model_key, k=1, dataset=items, prompt=EXTRACTION_PROMPT, runs_root=tmp_path
            )

        assert result_dir.path == run_dir.path
        artifact = load_run(result_dir)
        assert artifact.completed is True
        assert len(artifact.rows) == 2
        # 2 calls from the first run + 1 re-executed call for the item whose
        # trailing row got truncated -- the other item is never re-called.
        assert candidate.call_count == 3

    def test_load_run_also_tolerates_truncated_last_line(self, tmp_path):
        items = [make_item("item-0"), make_item("item-1")]
        model_key = _model_key()
        config = _config()

        run_dir = run_eval(
            config, model_key, k=1, dataset=items, prompt=EXTRACTION_PROMPT, runs_root=tmp_path
        )
        rows_path = run_dir.path / "rows.jsonl"
        lines = rows_path.read_text(encoding="utf-8").splitlines()
        truncated = lines[-1][: len(lines[-1]) // 2]
        rows_path.write_text(lines[0] + "\n" + truncated, encoding="utf-8")

        with pytest.warns(UserWarning):
            artifact = load_run(run_dir)

        assert len(artifact.rows) == 1

    def test_midfile_corruption_still_raises_on_resume_and_load(self, tmp_path):
        items = [make_item("item-0"), make_item("item-1"), make_item("item-2")]
        model_key = _model_key()
        config = _config()

        run_dir = run_eval(
            config, model_key, k=1, dataset=items, prompt=EXTRACTION_PROMPT, runs_root=tmp_path
        )
        rows_path = run_dir.path / "rows.jsonl"
        lines = rows_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        lines[1] = "{not valid json at all"
        rows_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with pytest.raises(json.JSONDecodeError):
            run_eval(
                config, model_key, k=1, dataset=items, prompt=EXTRACTION_PROMPT, runs_root=tmp_path
            )

        with pytest.raises(json.JSONDecodeError):
            load_run(run_dir)

    def test_stale_repair_tmp_is_cleaned_up_and_repair_completes(self, tmp_path):
        """Simulate the killed-mid-repair state: valid rows.jsonl + stale
        .repair-tmp with partial content. On resume, the stale tmp should be
        removed, rows.jsonl retains all valid rows, and repair completes."""
        items = [make_item("item-0"), make_item("item-1")]
        candidate = FakeModelClient(make_result=lambda *a: success_result())
        model_key = _model_key(candidate)
        config = _config()

        # First run to create valid rows.jsonl with 2 completed items.
        run_dir = run_eval(
            config, model_key, k=1, dataset=items, prompt=EXTRACTION_PROMPT, runs_root=tmp_path
        )
        assert candidate.call_count == 2

        rows_path = run_dir.path / "rows.jsonl"
        repair_tmp = rows_path.with_suffix(".jsonl.repair-tmp")

        # Read the valid rows.
        lines = rows_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2

        # Simulate the killed-mid-repair state: keep rows.jsonl intact, but
        # create a stale repair-tmp with partial content (as if a prior repair
        # was killed between writing the temp and the os.replace).
        repair_tmp.write_text(lines[0] + "\n", encoding="utf-8")

        # On resume, the stale tmp should be removed, rows.jsonl stays intact
        # with all valid rows, and everything completes successfully.
        result_dir = run_eval(
            config, model_key, k=1, dataset=items, prompt=EXTRACTION_PROMPT, runs_root=tmp_path
        )

        assert result_dir.path == run_dir.path
        assert not repair_tmp.exists(), "stale repair-tmp should be cleaned up"
        artifact = load_run(result_dir)
        assert artifact.completed is True
        assert len(artifact.rows) == 2
        # No additional candidate calls should be made since both rows
        # survived the stale tmp scenario.
        assert candidate.call_count == 2
