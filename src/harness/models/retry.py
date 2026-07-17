"""Transport-only retry decorator (spec §9).

Retries apply exclusively to transport-level failures -- 429 rate limits, 5xx
server errors, connection timeouts -- with jittered exponential backoff,
capped at ``max_attempts`` (config ``retry_max_attempts``) and at
``max_backoff`` seconds per individual wait. A response the transport
successfully returns is never retried, regardless of its content:
schema-invalid output and refusals are scored candidate failures (spec §6),
not retry triggers. Callers classify transport errors themselves via
``is_transport_error`` because the SDK-raised exception types differ per
provider (spec §2's three hand-written clients).

Hardened 2026-07-17: the judge provider's 503 load-shedding bursts (observed
live 2026-07-16/17) run 2-5 minutes -- longer than the original 4-attempt,
~30s total patience, which could abort an almost-complete run mid-burst.
``DEFAULT_MAX_BACKOFF_SECONDS`` caps each individual wait at 120s; paired
with ``retry_max_attempts: 12`` (``configs/default.yaml``), the backoff
schedule alone sums to ~487.5s (~8 minutes) worst case.

Corrected 2026-07-17 (same day): that ~8-minute figure counted backoff
sleeps only and understated the true worst case. httpx timeouts are
retryable by design (they satisfy ``is_transport_error``), and each
provider client sets an explicit ~100s per-request timeout (``cli.py``'s
``_REQUEST_TIMEOUT_SECONDS``). A hanging-connection burst therefore
compounds up to 12 request timeouts with the backoff waits between them --
12*100s + 487.5s =~ 28.1 minutes for a single call, which would floor an
entire CI run against the 30-minute job ceiling
(``.github/workflows/eval-gate.yml``) once the runner's worker pool waits
on that one in-flight future.

``PATIENCE_BUDGET_SECONDS`` (600s) fixes this: ``retry_transport`` tracks
wall-clock elapsed time from the first attempt and, before sleeping into
the next retry, raises ``TransportExhausted`` immediately if elapsed plus
the next planned wait would exceed the budget -- instead of always
retrying up to ``max_attempts``. Fast-fail bursts (429/5xx, no hang) are
unaffected: the full 487.5s backoff schedule fits comfortably inside the
600s budget, so those still get all 12 attempts. Hang-dominated bursts are
cut off after roughly 6 timeouts' worth (~600s) instead of 12. Worst-case
wall time for a single call is therefore provably bounded by budget + one
final request timeout: 600 + 100 = 700s =~ 11.7 minutes -- comfortably
inside CI's 30-minute job budget, with margin left for setup steps.
``max_attempts`` remains the secondary bound; non-retryable errors still
fail on the first attempt, exactly as before; this widens patience for
bursts, it never waits forever.
"""

from __future__ import annotations

import functools
import random
import time
from collections.abc import Callable
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")

DEFAULT_MAX_BACKOFF_SECONDS = 120.0

# Wall-clock patience budget, seconds (2026-07-17 correction -- see module
# docstring). Bounds total retry wall time even when attempts hang for their
# full per-request timeout instead of failing fast, independent of how many
# of ``max_attempts`` tries that would otherwise take.
PATIENCE_BUDGET_SECONDS = 600.0


class TransportExhausted(Exception):
    """Raised when a transport call still fails after ``max_attempts`` tries.

    Per spec §6, this is a measurement error: the run aborts rather than
    scoring a candidate failure.
    """

    def __init__(self, attempts: int, last_error: BaseException) -> None:
        super().__init__(f"Transport call failed after {attempts} attempt(s): {last_error!r}")
        self.attempts = attempts
        self.last_error = last_error


def retry_transport(
    max_attempts: int,
    is_transport_error: Callable[[BaseException], bool],
    *,
    base_delay: float = 0.5,
    max_backoff: float = DEFAULT_MAX_BACKOFF_SECONDS,
    patience_budget: float = PATIENCE_BUDGET_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
    now: Callable[[], float] = time.monotonic,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Decorator: retry ``fn`` only on exceptions ``is_transport_error`` accepts.

    Any other exception propagates immediately on the first attempt, without
    consuming a retry. Backoff between attempts grows exponentially in the
    attempt number (``base_delay * 2**(attempt - 1)``), capped at
    ``max_backoff`` seconds so a long attempt run doesn't keep doubling
    without bound, then scaled by ``jitter()`` (expected in ``[0, 1)``, e.g.
    ``random.random``). The ``max_attempts``th consecutive transport error
    raises ``TransportExhausted`` instead of retrying further.

    ``patience_budget`` is a second, wall-clock bound (2026-07-17
    correction): elapsed time is tracked via ``now()`` from the first
    attempt, and if elapsed plus the next planned wait would exceed
    ``patience_budget``, retrying stops immediately with
    ``TransportExhausted`` rather than sleeping again. This is what actually
    caps worst-case wall time when attempts hang for their full per-request
    timeout before failing -- a hanging connection is a retryable transport
    error, same as a fast 429/5xx, so it does not skip this accounting.
    ``max_attempts`` remains the secondary bound, for fast-failing bursts
    that never come close to the budget.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if max_backoff <= 0:
        raise ValueError("max_backoff must be > 0")
    if patience_budget <= 0:
        raise ValueError("patience_budget must be > 0")

    def decorator(fn: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            start = now()
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    if not is_transport_error(exc):
                        raise
                    if attempt >= max_attempts:
                        raise TransportExhausted(attempt, exc) from exc
                    delay = min(base_delay * (2 ** (attempt - 1)), max_backoff) * jitter()
                    elapsed = now() - start
                    if elapsed + delay > patience_budget:
                        raise TransportExhausted(attempt, exc) from exc
                    sleep(delay)
            raise AssertionError("unreachable: loop always returns or raises")

        return wrapper

    return decorator
