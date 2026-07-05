from harness.models.retry import TransportExhausted, retry_transport


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
