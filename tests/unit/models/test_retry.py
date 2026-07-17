import pytest

from harness.models.retry import (
    DEFAULT_MAX_BACKOFF_SECONDS,
    PATIENCE_BUDGET_SECONDS,
    TransportExhausted,
    retry_transport,
)


class FakeTransportError(Exception):
    """Stand-in for a provider SDK's 429/5xx/timeout exception."""


def _is_fake_transport_error(exc: BaseException) -> bool:
    return isinstance(exc, FakeTransportError)


class _FakeClock:
    """A wall clock that only moves when told to -- models real elapsed time
    without any real sleeping. ``advance`` doubles as the ``sleep`` seam (a
    sleep consumes wall time) and a fake ``fn`` can also call it directly to
    model time spent hung inside a request before it times out."""

    def __init__(self) -> None:
        self._elapsed = 0.0

    def advance(self, seconds: float) -> None:
        self._elapsed += seconds

    def now(self) -> float:
        return self._elapsed


class TestRetryTransportSucceedsWithinCap:
    def test_succeeds_after_two_transport_errors_in_three_attempts(self):
        calls = []

        @retry_transport(
            max_attempts=4, is_transport_error=_is_fake_transport_error, sleep=lambda _: None
        )
        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise FakeTransportError("429")
            return "ok"

        assert flaky() == "ok"
        assert len(calls) == 3


class TestRetryTransportExhaustion:
    def test_exhausts_after_max_attempts_transport_errors(self):
        calls = []

        @retry_transport(
            max_attempts=4, is_transport_error=_is_fake_transport_error, sleep=lambda _: None
        )
        def always_fails():
            calls.append(1)
            raise FakeTransportError("429")

        try:
            always_fails()
        except TransportExhausted:
            pass
        else:
            raise AssertionError("expected TransportExhausted")
        assert len(calls) == 4

    def test_transport_exhausted_wraps_last_error(self):
        @retry_transport(
            max_attempts=1, is_transport_error=_is_fake_transport_error, sleep=lambda _: None
        )
        def always_fails():
            raise FakeTransportError("boom")

        try:
            always_fails()
            raised = None
        except TransportExhausted as exc:
            raised = exc

        assert raised is not None
        assert raised.attempts == 1
        assert isinstance(raised.last_error, FakeTransportError)


class TestRetryTransportNonTransportErrors:
    def test_non_transport_error_is_raised_immediately_without_retry(self):
        calls = []

        @retry_transport(
            max_attempts=4, is_transport_error=_is_fake_transport_error, sleep=lambda _: None
        )
        def raises_value_error():
            calls.append(1)
            raise ValueError("not a transport error")

        try:
            raises_value_error()
            raised = None
        except ValueError as exc:
            raised = exc

        assert raised is not None
        assert len(calls) == 1

    def test_a_returned_response_is_never_re_sampled(self):
        """A successful call is never retried, regardless of content -- the
        decorator has no notion of the return value's validity (spec §9)."""
        calls = []

        @retry_transport(
            max_attempts=4, is_transport_error=_is_fake_transport_error, sleep=lambda _: None
        )
        def returns_junk():
            calls.append(1)
            return {"not": "validated here"}

        result = returns_junk()
        assert result == {"not": "validated here"}
        assert len(calls) == 1


class TestRetryTransportBackoff:
    def test_backoff_sleeps_between_attempts_with_increasing_delay(self):
        sleeps = []

        @retry_transport(
            max_attempts=3,
            is_transport_error=_is_fake_transport_error,
            sleep=sleeps.append,
            jitter=lambda: 1.0,
        )
        def always_fails():
            raise FakeTransportError("429")

        try:
            always_fails()
        except TransportExhausted:
            pass

        # three attempts -> two backoff sleeps between them, strictly increasing
        assert len(sleeps) == 2
        assert sleeps[0] < sleeps[1]
        assert all(s > 0 for s in sleeps)


class TestRetryTransportBackoffCap:
    """Hardening (2026-07-17): backoff must stop doubling once it reaches
    ``max_backoff`` rather than growing unbounded across many attempts --
    the load-shedding bursts this widens patience for run 2-5 minutes, so a
    handful of attempts easily reach an uncapped multi-hour delay otherwise."""

    def test_individual_wait_never_exceeds_max_backoff(self):
        sleeps = []

        @retry_transport(
            max_attempts=8,
            is_transport_error=_is_fake_transport_error,
            base_delay=0.5,
            max_backoff=10.0,
            sleep=sleeps.append,
            jitter=lambda: 1.0,  # worst case: no jitter shrinkage
        )
        def always_fails():
            raise FakeTransportError("503")

        try:
            always_fails()
        except TransportExhausted:
            pass

        # uncapped doubling from 0.5 would reach 0.5,1,2,4,8,16,32 -- the
        # last two must clamp to the 10.0 cap instead.
        assert len(sleeps) == 7
        assert sleeps == [0.5, 1.0, 2.0, 4.0, 8.0, 10.0, 10.0]
        assert all(s <= 10.0 for s in sleeps)

    def test_default_cap_is_120_seconds(self):
        sleeps = []

        @retry_transport(
            max_attempts=12,
            is_transport_error=_is_fake_transport_error,
            sleep=sleeps.append,
            jitter=lambda: 1.0,
        )
        def always_fails():
            raise FakeTransportError("503")

        try:
            always_fails()
        except TransportExhausted:
            pass

        assert max(sleeps) == DEFAULT_MAX_BACKOFF_SECONDS == 120.0
        assert all(s <= DEFAULT_MAX_BACKOFF_SECONDS for s in sleeps)

    def test_max_backoff_must_be_positive(self):
        with pytest.raises(ValueError, match="max_backoff"):
            retry_transport(
                max_attempts=3,
                is_transport_error=_is_fake_transport_error,
                max_backoff=0.0,
            )


class TestRetryTransportTotalPatience:
    """Hardening (2026-07-17): the configured default schedule
    (``retry_max_attempts: 12``, the module's ``DEFAULT_MAX_BACKOFF_SECONDS``
    cap) sums its backoff sleeps to roughly 8-10 minutes -- long enough to
    ride out an observed 2-5 minute 503 load-shedding burst, short enough to
    leave CI's ~30-minute job budget intact for a fast-failing burst. No
    real sleeping: durations are recorded instead of actually waited out.

    Correction (2026-07-17, same day): this class measures backoff-sleep
    time only, which is not the true worst case for a *hanging* burst --
    see ``TestRetryTransportPatienceBudget`` below for the compound bound
    (patience budget + one final request timeout) that actually governs a
    hang-dominated failure.
    """

    def test_worst_case_total_wait_is_bounded_between_8_and_10_minutes(self):
        sleeps = []

        @retry_transport(
            max_attempts=12,
            is_transport_error=_is_fake_transport_error,
            sleep=sleeps.append,
            jitter=lambda: 1.0,  # worst case: jitter never shrinks a wait
        )
        def always_fails():
            raise FakeTransportError("503")

        try:
            always_fails()
        except TransportExhausted:
            pass

        assert len(sleeps) == 11  # 12 attempts -> 11 backoff waits
        total_seconds = sum(sleeps)
        assert 8 * 60 <= total_seconds <= 10 * 60
        assert total_seconds == pytest.approx(487.5)

    def test_expected_case_with_realistic_jitter_is_well_under_the_cap(self):
        # random.random() lands roughly in the middle of [0, 1) on average --
        # a burst-riding retry should typically resolve in a few minutes, not
        # linger near the worst-case bound every time.
        sleeps = []

        @retry_transport(
            max_attempts=12,
            is_transport_error=_is_fake_transport_error,
            sleep=sleeps.append,
            jitter=lambda: 0.5,
        )
        def always_fails():
            raise FakeTransportError("503")

        try:
            always_fails()
        except TransportExhausted:
            pass

        assert sum(sleeps) == pytest.approx(487.5 / 2)


class TestRetryTransportPatienceBudget:
    """Correction (2026-07-17, same day): ``TestRetryTransportTotalPatience``
    above only bounds backoff-sleep time. httpx timeouts are themselves
    retryable transport errors (they satisfy ``is_transport_error``), and
    each provider client sets an explicit ~100s per-request timeout -- so a
    hanging-connection burst compounds up to 12 request timeouts with the
    backoff waits between them (12*100s + 487.5s =~ 28.1 minutes), which
    would floor an entire CI run against the 30-minute job ceiling.
    ``PATIENCE_BUDGET_SECONDS`` (600s) is a wall-clock bound, independent of
    ``max_attempts``, that cuts a hang-dominated burst off well before
    attempt 12 while leaving fast-fail bursts' full backoff schedule
    untouched. No real sleeping: ``_FakeClock`` records requested durations
    instead of waiting them out."""

    def test_budget_cutoff_fires_before_max_attempts_when_attempts_hang(self):
        clock = _FakeClock()
        calls = []

        @retry_transport(
            max_attempts=12,
            is_transport_error=_is_fake_transport_error,
            sleep=clock.advance,
            jitter=lambda: 1.0,  # worst case: jitter never shrinks a wait
            now=clock.now,
            patience_budget=600.0,
        )
        def hangs_then_times_out():
            calls.append(1)
            clock.advance(100.0)  # each attempt hangs for the full ~100s
            raise FakeTransportError("timeout")  # the request times out

        try:
            hangs_then_times_out()
        except TransportExhausted as exc:
            raised = exc
        else:
            raise AssertionError("expected TransportExhausted")

        # 100s/attempt exhausts the 600s budget around the 6th attempt,
        # well short of the 12-attempt cap that would otherwise apply.
        assert raised.attempts == 6
        assert len(calls) == 6

    def test_fast_fail_burst_still_traverses_the_full_backoff_schedule(self):
        """The budget must not shorten a fast-failing (no-hang) burst: the
        487.5s worst-case backoff schedule fits comfortably inside the 600s
        budget, so all 12 attempts still happen exactly as before this
        correction."""
        sleeps = []

        @retry_transport(
            max_attempts=12,
            is_transport_error=_is_fake_transport_error,
            sleep=sleeps.append,
            jitter=lambda: 1.0,  # worst case: jitter never shrinks a wait
            patience_budget=600.0,
        )
        def always_fails():
            raise FakeTransportError("503")

        try:
            always_fails()
        except TransportExhausted as exc:
            raised = exc
        else:
            raise AssertionError("expected TransportExhausted")

        assert raised.attempts == 12
        assert len(sleeps) == 11
        assert sum(sleeps) == pytest.approx(487.5)

    def test_patience_budget_must_be_positive(self):
        with pytest.raises(ValueError, match="patience_budget"):
            retry_transport(
                max_attempts=3,
                is_transport_error=_is_fake_transport_error,
                patience_budget=0.0,
            )

    def test_worst_case_single_call_wall_time_bound_is_budget_plus_one_timeout(self):
        """The true worst-case wall time for one call is patience_budget plus
        one final per-request timeout -- not the backoff schedule alone.
        ``client_request_timeout_seconds`` mirrors cli.py's
        ``_REQUEST_TIMEOUT_SECONDS`` (100.0), kept as a literal here so this
        bound stays self-contained in a models-level test rather than
        importing the CLI module."""
        client_request_timeout_seconds = 100.0
        worst_case_wall_seconds = PATIENCE_BUDGET_SECONDS + client_request_timeout_seconds

        assert worst_case_wall_seconds == pytest.approx(700.0)
        assert worst_case_wall_seconds / 60 == pytest.approx(11.6666667, rel=1e-6)
        assert worst_case_wall_seconds < 30 * 60  # clears the CI job ceiling with margin
