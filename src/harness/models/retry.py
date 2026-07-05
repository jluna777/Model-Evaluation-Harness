"""Transport-only retry decorator (spec §9).

Retries apply exclusively to transport-level failures -- 429 rate limits, 5xx
server errors, connection timeouts -- with jittered exponential backoff,
capped at ``max_attempts`` (config ``retry_max_attempts``). A response the
transport successfully returns is never retried, regardless of its content:
schema-invalid output and refusals are scored candidate failures (spec §6),
not retry triggers. Callers classify transport errors themselves via
``is_transport_error`` because the SDK-raised exception types differ per
provider (spec §2's three hand-written clients).
"""

from __future__ import annotations

import functools
import random
import time
from collections.abc import Callable
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


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
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Decorator: retry ``fn`` only on exceptions ``is_transport_error`` accepts.

    Any other exception propagates immediately on the first attempt, without
    consuming a retry. Backoff between attempts grows exponentially in the
    attempt number (``base_delay * 2**(attempt - 1)``) scaled by ``jitter()``
    (expected in ``[0, 1)``, e.g. ``random.random``). The ``max_attempts``th
    consecutive transport error raises ``TransportExhausted`` instead of
    retrying further.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

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
                    delay = base_delay * (2 ** (attempt - 1)) * jitter()
                    sleep(delay)
            raise AssertionError("unreachable: loop always returns or raises")

        return wrapper

    return decorator
