import pytest

from harness.models.retry import DEFAULT_MAX_BACKOFF_SECONDS, TransportExhausted, retry_transport


class FakeTransportError(Exception):
    """Stand-in for a provider SDK's 429/5xx/timeout exception."""


def _is_fake_transport_error(exc: BaseException) -> bool:
    return isinstance(exc, FakeTransportError)


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
    cap) must bound worst-case total wait to roughly 8-10 minutes -- long
    enough to ride out an observed 2-5 minute 503 load-shedding burst,
    short enough to leave CI's ~30-minute job budget intact. No real
    sleeping: a fake clock records requested durations instead."""

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
