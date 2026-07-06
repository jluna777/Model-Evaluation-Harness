"""Tests for Langfuse tracing with bounded degradation (spec §8, T09).

All tests use a hand-written fake Langfuse transport (``FakeLangfuseClient``
below) -- structurally the same call shape as the installed ``langfuse``
v3.15.0 SDK's ``Langfuse.start_span``/``LangfuseSpan.end``/``.score()`` (see
``src/harness/tracing.py`` module docstring for the exact API surface
consulted) -- so nothing here ever performs a live Langfuse call. The real
``langfuse.Langfuse`` class is only ever constructed on the production path
inside ``TraceContext.for_run`` when no ``client_factory`` is supplied, which
no test here does.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from harness.config import Config, load_config
from harness.judge.judge import JudgeVerdict
from harness.models import StructuredResult, Usage
from harness.prompts import EXTRACTION_PROMPT
from harness.runner import ModelKey, load_run, run_eval
from harness.schema import EmailInput, GoldenExpected, GoldenItem, GoldenMeta
from harness.tracing import MissingTracingError, TraceContext

DEFAULT_CONFIG_PATH = Path(__file__).parents[2] / "configs" / "default.yaml"


def _config() -> Config:
    return load_config(DEFAULT_CONFIG_PATH)


def make_item(item_id: str) -> GoldenItem:
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
            slice="nominal",
            categories=["billing"],
            difficulty=1,
            generator="gpt-4",
            edited=False,
            notes="",
        ),
    )


def success_result() -> StructuredResult:
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
        served_model_version="candidate-v1",
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
    def complete_structured(self, prompt: str, schema: type) -> StructuredResult:
        if schema.__name__ == "JudgeVerdict":
            return judge_pass_result()
        return success_result()


def _model_key() -> ModelKey:
    return ModelKey(label="a", candidate_client=FakeModelClient(), judge_client=FakeModelClient())


@dataclass
class FakeSpan:
    """Structurally matches ``langfuse._client.span.LangfuseObservationWrapper``'s
    ``.end()``/``.score()`` surface -- see ``LangfuseSpan`` in the installed
    SDK (``langfuse/_client/span.py``)."""

    name: str
    metadata: dict
    trace_context: dict | None
    _client: FakeLangfuseClient
    ended: bool = False
    scores: list[tuple[str, float]] = field(default_factory=list)

    def end(self) -> None:
        self.ended = True

    def score(self, *, name: str, value: float, data_type: str | None = None) -> None:
        self.scores.append((name, value))


@dataclass
class FakeLangfuseClient:
    """Fake Langfuse transport double (no network, ever).

    ``fail_after`` -- once ``fail_after`` spans have been successfully
    started, every subsequent ``start_span`` call raises, simulating a
    mid-run Langfuse transport failure (spec §8) without a live client or a
    real network call. A span that already started successfully always
    finishes cleanly (``.end()``/``.score()`` never fail) -- only *new*
    span creation is where this fake models the transport breaking.
    """

    fail_after: int | None = None
    spans: list[FakeSpan] = field(default_factory=list)
    flush_calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def start_span(self, *, name, trace_context=None, metadata=None):
        with self._lock:
            if self.fail_after is not None and len(self.spans) >= self.fail_after:
                raise RuntimeError("simulated Langfuse transport failure (start_span)")
            span = FakeSpan(
                name=name, metadata=dict(metadata or {}), trace_context=trace_context, _client=self
            )
            self.spans.append(span)
            return span

    def flush(self) -> None:
        self.flush_calls += 1


class TestMissingTracingErrorFailsFast:
    """Acceptance anchor 1: reportable=True + missing keys -> MissingTracingError
    at startup, before any candidate/judge call (fake client untouched)."""

    def test_reportable_true_missing_keys_raises_before_any_call(self, monkeypatch):
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
        fake_client = FakeLangfuseClient()

        with pytest.raises(MissingTracingError):
            TraceContext.for_run(_config(), reportable=True, client_factory=lambda: fake_client)

        assert fake_client.spans == []

    def test_reportable_true_missing_only_public_key_still_raises(self, monkeypatch):
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

        with pytest.raises(MissingTracingError):
            TraceContext.for_run(_config(), reportable=True)

    def test_reportable_true_with_keys_present_does_not_raise(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        fake_client = FakeLangfuseClient()

        trace = TraceContext.for_run(_config(), reportable=True, client_factory=lambda: fake_client)

        assert trace.untraced is False


class TestKeylessDevRunProceeds:
    """Acceptance anchor 2: reportable=False + missing keys proceeds with
    exactly one warning and untraced=True."""

    def test_reportable_false_missing_keys_warns_once_and_is_untraced(self, monkeypatch):
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

        with pytest.warns(UserWarning) as record:
            trace = TraceContext.for_run(_config(), reportable=False)

        assert len(record) == 1
        assert trace.untraced is True

    def test_reportable_false_missing_keys_never_touches_client_factory(self, monkeypatch):
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
        calls = []

        def factory():
            calls.append(1)
            return FakeLangfuseClient()

        with pytest.warns(UserWarning):
            TraceContext.for_run(_config(), reportable=False, client_factory=factory)

        assert calls == []


class TestSpanShapeAndScoring:
    """Acceptance anchor 4: with keys present and a working fake transport,
    spans form one per-run trace tagged with run id + fingerprint; one span
    per candidate call and one per judge call, each carrying item id and
    replicate index; scores attached per item."""

    def test_spans_and_scores_carry_spec_metadata(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        fake_client = FakeLangfuseClient()

        trace = TraceContext.for_run(
            _config(),
            reportable=True,
            run_id="run-abc",
            fingerprint="fp-123",
            client_factory=lambda: fake_client,
        )

        with trace.candidate_span(item_id="item-0", replicate=0):
            pass
        with trace.judge_span(item_id="item-0", replicate=0, field="issue_summary"):
            pass
        with trace.judge_span(item_id="item-0", replicate=0, field="requested_action"):
            pass
        trace.record_item_scores(
            item_id="item-0",
            replicate=0,
            field_scores={"issue_summary": 1, "requested_action": 0, "category": None},
        )

        assert trace.untraced is False
        candidate_spans = [s for s in fake_client.spans if s.name == "candidate"]
        judge_spans = [s for s in fake_client.spans if s.name.startswith("judge:")]
        score_spans = [s for s in fake_client.spans if s.name == "scores"]

        assert len(candidate_spans) == 1
        assert len(judge_spans) == 2
        assert len(score_spans) == 1
        assert all(span.ended for span in fake_client.spans)

        # one per-run trace tagged with run id + fingerprint
        for span in fake_client.spans:
            assert span.trace_context == {"trace_id": trace._trace_id}
            assert span.metadata["run_id"] == "run-abc"
            assert span.metadata["fingerprint"] == "fp-123"

        for span in candidate_spans + judge_spans:
            assert span.metadata["item_id"] == "item-0"
            assert span.metadata["replicate"] == 0

        # scores attached per item; a missing verdict (None) is never scored
        assert score_spans[0].scores == [("issue_summary", 1.0), ("requested_action", 0.0)]


class TestMidRunTransportFailureBoundedDegradation:
    """Acceptance anchor 3: a fake transport that starts failing mid-run must
    never raise out of TraceContext -- it degrades to untraced instead."""

    def test_failure_after_first_span_marks_untraced_without_raising(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        fake_client = FakeLangfuseClient(fail_after=1)

        trace = TraceContext.for_run(_config(), reportable=True, client_factory=lambda: fake_client)

        with trace.candidate_span(item_id="item-0", replicate=0):
            pass
        assert trace.untraced is False

        with pytest.warns(UserWarning):
            with trace.candidate_span(item_id="item-1", replicate=0):
                pass  # must not raise even though the fake now raises internally

        assert trace.untraced is True

        # once untraced, further calls are silent no-ops (no repeat warnings)
        with trace.judge_span(item_id="item-1", replicate=0, field="issue_summary"):
            pass
        trace.record_item_scores(item_id="item-1", replicate=0, field_scores={"issue_summary": 1})

    def test_span_end_failure_marks_untraced_without_raising(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

        class FailingEndSpan:
            def end(self) -> None:
                raise RuntimeError("simulated Langfuse transport failure (end)")

            def score(self, *, name, value, data_type=None) -> None:
                pass

        class FailingEndClient:
            def start_span(self, *, name, trace_context=None, metadata=None):
                return FailingEndSpan()

            def flush(self) -> None:
                pass

        trace = TraceContext.for_run(
            _config(), reportable=True, client_factory=lambda: FailingEndClient()
        )

        with pytest.warns(UserWarning):
            with trace.candidate_span(item_id="item-0", replicate=0):
                pass  # the body itself does nothing; span.end() raises on exit

        assert trace.untraced is True

    def test_flush_failure_marks_untraced_without_raising(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

        class FailingFlushClient(FakeLangfuseClient):
            def flush(self) -> None:
                raise RuntimeError("simulated flush failure")

        trace = TraceContext.for_run(
            _config(), reportable=True, client_factory=lambda: FailingFlushClient()
        )

        with pytest.warns(UserWarning):
            trace.flush()

        assert trace.untraced is True


class TestRunnerIntegration:
    """The runner (T08) wires candidate/judge calls into spans when a
    ``TraceContext`` is supplied, and stamps ``untraced`` into the persisted
    manifest/artifact either way (spec §8)."""

    def test_no_trace_context_supplied_is_untraced_by_default(self, tmp_path):
        run_dir = run_eval(
            _config(),
            _model_key(),
            k=1,
            dataset=[make_item("item-0")],
            prompt=EXTRACTION_PROMPT,
            runs_root=tmp_path,
        )
        artifact = load_run(run_dir)

        assert artifact.completed is True
        assert artifact.untraced is True

    def test_working_trace_context_produces_spans_and_traced_artifact(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        fake_client = FakeLangfuseClient()
        config = _config()
        trace = TraceContext.for_run(
            config, reportable=True, run_id="run-xyz", client_factory=lambda: fake_client
        )

        run_dir = run_eval(
            config,
            _model_key(),
            k=1,
            dataset=[make_item("item-0")],
            prompt=EXTRACTION_PROMPT,
            runs_root=tmp_path,
            trace=trace,
        )
        artifact = load_run(run_dir)

        assert artifact.completed is True
        assert artifact.untraced is False
        candidate_spans = [s for s in fake_client.spans if s.name == "candidate"]
        judge_spans = [s for s in fake_client.spans if s.name.startswith("judge:")]
        score_spans = [s for s in fake_client.spans if s.name == "scores"]
        assert len(candidate_spans) == 1
        assert len(judge_spans) == 2  # issue_summary + requested_action
        assert len(score_spans) == 1
        assert fake_client.flush_calls == 1

    def test_keyless_dev_run_through_run_eval_is_untraced_with_one_warning(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
        config = _config()

        with pytest.warns(UserWarning) as record:
            trace = TraceContext.for_run(config, reportable=False)
        assert len(record) == 1

        run_dir = run_eval(
            config,
            _model_key(),
            k=1,
            dataset=[make_item("item-0")],
            prompt=EXTRACTION_PROMPT,
            runs_root=tmp_path,
            trace=trace,
        )
        artifact = load_run(run_dir)

        assert artifact.completed is True
        assert artifact.untraced is True

    def test_mid_run_transport_failure_completes_run_flagged_untraced(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        fake_client = FakeLangfuseClient(fail_after=1)
        config = _config()
        trace = TraceContext.for_run(config, reportable=True, client_factory=lambda: fake_client)

        items = [make_item(f"item-{i}") for i in range(5)]

        with pytest.warns(UserWarning):
            run_dir = run_eval(
                config,
                _model_key(),
                k=1,
                dataset=items,
                prompt=EXTRACTION_PROMPT,
                runs_root=tmp_path,
                trace=trace,
                max_workers=1,
            )
        artifact = load_run(run_dir)

        # measurement is unaffected: the run completes with every item scored
        assert artifact.completed is True
        assert len(artifact.rows) == 5
        assert all(row.field_scores["issue_summary"] is not None for row in artifact.rows)
        # ... but the artifact is flagged untraced (spec §8)
        assert artifact.untraced is True

    def test_untraced_flag_is_sticky_untraced_then_traced(self, monkeypatch, tmp_path):
        """Regression test: untraced flag is sticky once True.

        Invocation 1: run with trace=None (untraced=True in manifest)
        Invocation 2: resume same run with a working TraceContext (untraced should stay True)
        """
        config = _config()
        items = [make_item("item-0"), make_item("item-1")]

        # First invocation: no trace, run completes
        run_dir = run_eval(
            config,
            _model_key(),
            k=1,
            dataset=items,
            prompt=EXTRACTION_PROMPT,
            runs_root=tmp_path,
            trace=None,
        )
        artifact1 = load_run(run_dir)
        assert artifact1.untraced is True
        assert artifact1.completed is True
        assert len(artifact1.rows) == 2

        # Second invocation: resume same run with a working trace context
        # The dataset is identical, so all items are already completed. When the
        # manifest is rewritten, it should merge untraced=True from the prior
        # invocation with untraced=False from the current trace context, resulting
        # in untraced=True (sticky).
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        fake_client = FakeLangfuseClient()
        trace = TraceContext.for_run(
            config, reportable=True, client_factory=lambda: fake_client
        )

        run_dir2 = run_eval(
            config,
            _model_key(),
            k=1,
            dataset=items,  # Same dataset as first invocation
            prompt=EXTRACTION_PROMPT,
            runs_root=tmp_path,
            trace=trace,
        )
        artifact2 = load_run(run_dir2)
        # The flag is sticky: once untraced=True, it stays True even with a working trace
        assert artifact2.untraced is True
        assert len(artifact2.rows) == 2  # Same items as first invocation

    def test_untraced_flag_is_sticky_traced_then_untraced(self, monkeypatch, tmp_path):
        """Regression test: untraced flag is sticky once True.

        Invocation 1: run with a working TraceContext (untraced=False in manifest)
        Invocation 2: resume same run dir with trace=None (untraced should become True via OR)
        """
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        config = _config()

        # First invocation: working trace, run completes
        fake_client = FakeLangfuseClient()
        trace = TraceContext.for_run(
            config, reportable=True, client_factory=lambda: fake_client
        )

        run_dir = run_eval(
            config,
            _model_key(),
            k=1,
            dataset=[make_item("item-0")],
            prompt=EXTRACTION_PROMPT,
            runs_root=tmp_path,
            trace=trace,
        )
        artifact1 = load_run(run_dir)
        assert artifact1.untraced is False
        assert artifact1.completed is True

        # Second invocation: resume same run dir with no trace context
        # by adding a new item to the dataset
        run_dir2 = run_eval(
            config,
            _model_key(),
            k=1,
            dataset=[make_item("item-0"), make_item("item-1")],
            prompt=EXTRACTION_PROMPT,
            runs_root=tmp_path,
            trace=None,
        )
        artifact2 = load_run(run_dir2)
        # Second invocation is untraced, so untraced flag becomes True via OR
        assert artifact2.untraced is True
        assert len(artifact2.rows) == 2  # Both items now complete
