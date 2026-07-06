"""Langfuse tracing with bounded degradation (spec §8).

``TraceContext.for_run(config, reportable)`` is the single entry point:

- ``reportable=True`` with ``LANGFUSE_PUBLIC_KEY``/``LANGFUSE_SECRET_KEY``
  absent from the environment raises ``MissingTracingError`` at startup --
  before any candidate or judge call is made (the caller is expected to
  construct a ``TraceContext`` before touching any ``ModelClient``).
- ``reportable=False`` with keys absent proceeds keylessly: exactly one
  warning is emitted and the returned context is permanently ``untraced``
  (no Langfuse client is even constructed -- there is no credential to use).
- With keys present, a real (or, in tests, injected fake) Langfuse client is
  used to emit spans. Any exception raised by that client at any point --
  the bounded-degradation contract -- is caught, converted into exactly one
  warning, and flips the context to ``untraced`` for the remainder of the
  run. Tracing failures are never allowed to abort or corrupt a run:
  ``candidate_span``/``judge_span``/``record_item_scores``/``flush`` never
  raise.

**SDK surface consulted (langfuse v3.15.0, OpenTelemetry-based, read from
``.venv/Lib/site-packages/langfuse`` -- the installed package, not
documentation, per T09's SDK-accuracy note):**

- ``langfuse.Langfuse(*, public_key, secret_key, host, ...)`` --
  ``_client/client.py``. Keyless construction never happens here: this
  module reads and validates the two credential environment variables
  itself (so ``MissingTracingError`` fires before any client, real or fake,
  is even built), rather than relying on the SDK's own keyless behavior
  (which logs a warning and silently degrades to a no-op tracer instead of
  raising -- not the fail-fast contract this ticket needs).
- ``Langfuse.start_span(*, name, trace_context=None, metadata=None, ...) ->
  LangfuseSpan`` and ``Langfuse.flush()`` -- ``_client/client.py``.
- ``LangfuseSpan.end()`` and ``LangfuseSpan.score(*, name, value,
  data_type=None, ...)`` -- ``_client/span.py`` (``score`` attaches a score
  to that specific span/observation; ``LangfuseSpanProcessor`` -- the actual
  network transport -- is what a "Langfuse transport failure" means at
  runtime, but nothing here depends on that processor's internals: any
  exception from ``start_span``/``.end()``/``.score()``/``flush()``, for any
  reason, is treated identically).
- ``langfuse.types.TraceContext`` (a ``TypedDict`` of ``trace_id`` +
  optional ``parent_span_id``, passed as ``start_span(trace_context=...)``)
  is how this module fans candidate/judge calls for one run out across
  ``run_eval``'s thread pool (T08) into a single Langfuse trace without
  relying on OpenTelemetry's context-var-based "current span" propagation,
  which is per-thread and would silently split one run into several traces
  under concurrency. ``_new_trace_id`` mints that id locally (a valid
  32-hex-char OTEL trace id) -- deterministic from ``run_id`` when given, so
  re-running the identical call is traceable to the same id, random
  otherwise.

**Test-injection seam:** ``for_run`` accepts an additive, keyword-only
``client_factory`` (default ``None`` -> construct the real ``Langfuse``
client). Production callers never pass it; every test in
``tests/unit/test_tracing.py`` does, injecting a hand-written fake that
implements the same ``start_span``/``.end()``/``.score()``/``flush()`` call
shape -- so no test here ever performs a live Langfuse call, while the
production path still exercises the installed SDK's genuine public API.

**Span/trace shape (spec §8):** every candidate and judge call becomes its
own span (``candidate_span``/``judge_span``), named ``"candidate"`` /
``"judge:{field}"``, carrying ``item_id`` and ``replicate`` in its metadata.
All spans for one run share one Langfuse trace (via the ``trace_context``
mechanism above), tagged with ``run_id``/``fingerprint`` in every span's
metadata. Once a row's field scores are known, ``record_item_scores``
attaches them -- one Langfuse score per scored field, skipping fields with
no verdict (``None`` -- spec §7's judge-error convention, never coerced to a
score) -- to a single per-item span named ``"scores"``.
"""

from __future__ import annotations

import os
import secrets
import threading
import warnings
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from harness.config import Config

LANGFUSE_PUBLIC_KEY_ENV = "LANGFUSE_PUBLIC_KEY"
LANGFUSE_SECRET_KEY_ENV = "LANGFUSE_SECRET_KEY"


class MissingTracingError(Exception):
    """Raised by ``TraceContext.for_run`` at startup -- before any candidate
    or judge call -- when a reportable run (``reportable=True``) is
    requested without Langfuse credentials in the environment (spec §8):
    baseline updates, README/published numbers, and calibration
    certification all require complete traces and must fail fast rather
    than produce numbers that can never be attached to a trace."""


class SpanLike(Protocol):
    """The subset of ``langfuse._client.span.LangfuseObservationWrapper``
    (``LangfuseSpan`` in the installed SDK) this module calls."""

    def end(self) -> object: ...

    def score(self, *, name: str, value: float, data_type: str | None = None) -> None: ...


class LangfuseClientLike(Protocol):
    """The subset of ``langfuse.Langfuse``'s public interface this module
    calls -- real production instances and test fakes are both duck-typed
    against this, never inheritance."""

    def start_span(
        self,
        *,
        name: str,
        trace_context: Mapping[str, str] | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> SpanLike: ...

    def flush(self) -> None: ...


def _new_trace_id(run_id: str | None) -> str:
    """A valid 32-hex-char OTEL trace id for ``start_span(trace_context=...)``
    -- deterministic from ``run_id`` when given (stable across a call that
    supplies the same run id), random otherwise."""

    if run_id is not None:
        return sha256(run_id.encode("utf-8")).hexdigest()[:32]
    return secrets.token_hex(16)


def _build_default_client(config: Config, public_key: str, secret_key: str) -> LangfuseClientLike:
    """Constructs the real Langfuse client (production path only -- no test
    reaches this, since every test supplies ``client_factory``)."""

    from langfuse import Langfuse

    return Langfuse(public_key=public_key, secret_key=secret_key, host=config.langfuse.host)


@dataclass
class TraceContext:
    """Bounded-degradation handle around one run's Langfuse tracing (spec
    §8). Construct via ``for_run`` -- never directly."""

    _client: LangfuseClientLike | None
    untraced: bool
    run_id: str | None = None
    fingerprint: str | None = None
    _trace_id: str = field(default_factory=lambda: _new_trace_id(None))
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @classmethod
    def for_run(
        cls,
        config: Config,
        reportable: bool,
        *,
        run_id: str | None = None,
        fingerprint: str | None = None,
        client_factory: Callable[[], LangfuseClientLike] | None = None,
    ) -> TraceContext:
        """Construct the tracing context for one run.

        ``run_id``/``fingerprint`` are opaque strings tagged onto every span
        this context creates -- ``for_run`` does not compute them itself
        (the caller, typically the runner, owns run identity, spec §7).
        ``client_factory`` is an additive test-injection seam (default
        ``None`` -> construct the real ``langfuse.Langfuse`` client); no
        production call site passes it.
        """

        public_key = os.environ.get(LANGFUSE_PUBLIC_KEY_ENV)
        secret_key = os.environ.get(LANGFUSE_SECRET_KEY_ENV)
        keys_present = bool(public_key) and bool(secret_key)

        if reportable and not keys_present:
            raise MissingTracingError(
                "Reportable runs require Langfuse credentials (spec §8): set "
                f"{LANGFUSE_PUBLIC_KEY_ENV} and {LANGFUSE_SECRET_KEY_ENV} in the "
                "environment, or pass reportable=False for keyless dev iteration "
                "(that run can never feed a baseline or the README)."
            )

        trace_id = _new_trace_id(run_id)

        if not keys_present:
            # Keyless dev run (spec §8): proceed, one-line warning, permanently
            # untraced -- no Langfuse client is constructed at all, so
            # `client_factory` (if a caller mistakenly supplied one anyway) is
            # never even invoked.
            warnings.warn(
                "No Langfuse credentials found (LANGFUSE_PUBLIC_KEY/"
                "LANGFUSE_SECRET_KEY) -- proceeding untraced. This is fine for "
                "dev iteration, but the run can never feed a baseline or the "
                "README (spec §8).",
                stacklevel=2,
            )
            return cls(
                _client=None,
                untraced=True,
                run_id=run_id,
                fingerprint=fingerprint,
                _trace_id=trace_id,
            )

        client = (
            client_factory()
            if client_factory is not None
            else _build_default_client(config, public_key, secret_key)  # type: ignore[arg-type]
        )
        return cls(
            _client=client,
            untraced=False,
            run_id=run_id,
            fingerprint=fingerprint,
            _trace_id=trace_id,
        )

    def _metadata(self, **kwargs: object) -> dict[str, object]:
        meta: dict[str, object] = {"run_id": self.run_id, "fingerprint": self.fingerprint}
        meta.update(kwargs)
        return meta

    def _mark_untraced(self, exc: BaseException) -> None:
        """Bounded degradation (spec §8): any Langfuse failure, at any point,
        flips this context to untraced for the rest of the run -- warned
        exactly once (the first transition), silent thereafter -- and never
        re-raises. Thread-safe: ``run_eval`` (T08) drives candidate/judge
        calls from a worker pool, so multiple threads may hit a failing
        transport at the same time."""

        with self._lock:
            if self.untraced:
                return
            self.untraced = True
        warnings.warn(
            f"Langfuse tracing failed mid-run ({exc!r}); continuing untraced. "
            "Measurement is unaffected, but this run is flagged untraced and "
            "can never feed a baseline or the README (spec §8).",
            stacklevel=3,
        )

    @contextmanager
    def _span(self, name: str, metadata: dict[str, object]) -> Iterator[None]:
        if self.untraced or self._client is None:
            yield
            return

        span: SpanLike | None = None
        try:
            span = self._client.start_span(
                name=name, trace_context={"trace_id": self._trace_id}, metadata=metadata
            )
        except Exception as exc:  # any transport failure degrades, never aborts (spec §8)
            self._mark_untraced(exc)

        try:
            yield
        finally:
            if span is not None:
                try:
                    span.end()
                except Exception as exc:
                    self._mark_untraced(exc)

    def candidate_span(self, *, item_id: str, replicate: int) -> AbstractContextManager[None]:
        """Context manager wrapping one candidate call (spec §8: one span
        per call). No-op if this context is untraced."""

        return self._span("candidate", self._metadata(item_id=item_id, replicate=replicate))

    def judge_span(
        self, *, item_id: str, replicate: int, field: str
    ) -> AbstractContextManager[None]:
        """Context manager wrapping one judge call for one field (spec §8:
        one span per call). No-op if this context is untraced."""

        return self._span(
            f"judge:{field}",
            self._metadata(item_id=item_id, replicate=replicate, field=field),
        )

    def record_item_scores(
        self, *, item_id: str, replicate: int, field_scores: Mapping[str, int | None]
    ) -> None:
        """Attach this item's field scores to a single per-item Langfuse
        span (spec §8: scores attached per item). Fields with no verdict
        (``None`` -- a judge error, spec §7) are never scored. A no-op if
        this context is untraced."""

        if self.untraced or self._client is None:
            return

        try:
            span = self._client.start_span(
                name="scores",
                trace_context={"trace_id": self._trace_id},
                metadata=self._metadata(item_id=item_id, replicate=replicate),
            )
        except Exception as exc:
            self._mark_untraced(exc)
            return

        try:
            for field_name, value in field_scores.items():
                if value is None:
                    continue
                span.score(name=field_name, value=float(value), data_type="NUMERIC")
        except Exception as exc:
            self._mark_untraced(exc)
        finally:
            try:
                span.end()
            except Exception as exc:
                self._mark_untraced(exc)

    def flush(self) -> None:
        """Best-effort flush of any buffered Langfuse data. Never raises --
        a flush failure degrades to untraced like any other transport
        failure (spec §8)."""

        if self.untraced or self._client is None:
            return
        try:
            self._client.flush()
        except Exception as exc:
            self._mark_untraced(exc)
