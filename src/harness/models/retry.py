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
with ``retry_max_attempts: 12`` (``configs/default.yaml``), worst-case total
wait before ``TransportExhausted`` is ~8 minutes -- comfortably inside CI's
30-minute job budget. Non-retryable errors still fail on the first attempt,
exactly as before; this widens patience for bursts, it never waits forever.
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
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Decorator: retry ``fn`` only on exceptions ``is_transport_error`` accepts.

    Any other exception propagates immediately on the first attempt, without
    consuming a retry. Backoff between attempts grows exponentially in the
    attempt number (``base_delay * 2**(attempt - 1)``), capped at
    ``max_backoff`` seconds so a long attempt run doesn't keep doubling
    without bound, then scaled by ``jitter()`` (expected in ``[0, 1)``, e.g.
    ``random.random``). The ``max_attempts``th consecutive transport error
    raises ``TransportExhausted`` instead of retrying further.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if max_backoff <= 0:
        raise ValueError("max_backoff must be > 0")

    def decorator(fn: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    if not is_transport_error(exc):
                        raise
                    if attempt >= max_attempts:
                        raise TransportExhausted(attempt, exc) from exc
                    delay = min(base_delay * (2 ** (attempt - 1)), max_backoff) * jitter()
                    sleep(delay)
            raise AssertionError("unreachable: loop always returns or raises")

        return wrapper

    return decorator
