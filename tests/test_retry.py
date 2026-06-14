"""Tests for utils.retry — exponential_backoff, retry_sync, CircuitBreaker."""
from __future__ import annotations

import threading
import time
import unittest

from utils.retry import (
    CircuitBreaker,
    CircuitOpenError,
    exponential_backoff,
    retry_sync,
)


class ExponentialBackoffTests(unittest.TestCase):
    def test_first_attempt_equals_base(self):
        # With jitter=0, attempt 1 should return exactly base.
        d = exponential_backoff(1, base=1.0, cap=30.0, jitter=0.0)
        self.assertAlmostEqual(d, 1.0, places=9)

    def test_second_attempt_doubles(self):
        d = exponential_backoff(2, base=1.0, cap=30.0, jitter=0.0)
        self.assertAlmostEqual(d, 2.0, places=9)

    def test_third_attempt_quadruples(self):
        d = exponential_backoff(3, base=1.0, cap=30.0, jitter=0.0)
        self.assertAlmostEqual(d, 4.0, places=9)

    def test_capped_at_max_delay(self):
        # High attempt number should not exceed cap.
        d = exponential_backoff(20, base=1.0, cap=10.0, jitter=0.0)
        self.assertLessEqual(d, 10.0)

    def test_jitter_within_bounds(self):
        # With jitter=0.25, result should be within [0, cap].
        for attempt in range(1, 6):
            for _ in range(50):
                d = exponential_backoff(attempt, base=1.0, cap=30.0, jitter=0.25)
                self.assertGreaterEqual(d, 0.0)
                self.assertLessEqual(d, 30.0)

    def test_zero_base_returns_zero(self):
        d = exponential_backoff(1, base=0.0, cap=30.0, jitter=0.0)
        self.assertAlmostEqual(d, 0.0, places=9)

    def test_result_is_non_negative(self):
        # Even with extreme jitter, result must not go below 0.
        for _ in range(100):
            d = exponential_backoff(1, base=0.001, cap=30.0, jitter=0.25)
            self.assertGreaterEqual(d, 0.0)


class RetrySyncTests(unittest.TestCase):
    def test_succeeds_on_first_attempt(self):
        calls = []

        def fn():
            calls.append(1)
            return "ok"

        result = retry_sync(fn, max_attempts=3, base_delay=0, jitter=0)
        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 1)

    def test_retries_on_retryable_exception(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise ValueError("transient")
            return "done"

        result = retry_sync(
            fn, max_attempts=5, base_delay=0, jitter=0, retryable=ValueError
        )
        self.assertEqual(result, "done")
        self.assertEqual(len(calls), 3)

    def test_non_retryable_exception_propagates_immediately(self):
        calls = []

        def fn():
            calls.append(1)
            raise TypeError("non-retryable")

        with self.assertRaises(TypeError):
            retry_sync(
                fn, max_attempts=5, base_delay=0, jitter=0, retryable=ValueError
            )
        # Should only have been called once.
        self.assertEqual(len(calls), 1)

    def test_exhausts_max_attempts_and_re_raises(self):
        calls = []

        def fn():
            calls.append(1)
            raise ValueError("always fails")

        with self.assertRaises(ValueError):
            retry_sync(fn, max_attempts=3, base_delay=0, jitter=0, retryable=ValueError)
        self.assertEqual(len(calls), 3)

    def test_on_retry_callback_called(self):
        retries = []

        def fn():
            raise RuntimeError("x")

        def on_retry(attempt, exc, delay):
            retries.append((attempt, type(exc).__name__, delay))

        with self.assertRaises(RuntimeError):
            retry_sync(
                fn,
                max_attempts=3,
                base_delay=0,
                jitter=0,
                on_retry=on_retry,
            )
        # Called twice: after attempts 1 and 2 (not after the final attempt).
        self.assertEqual(len(retries), 2)
        self.assertEqual(retries[0][0], 1)
        self.assertEqual(retries[1][0], 2)

    def test_passes_args_and_kwargs(self):
        def fn(a, b, *, c):
            return (a, b, c)

        result = retry_sync(fn, 1, 2, c=3, max_attempts=1)
        self.assertEqual(result, (1, 2, 3))

    def test_tuple_retryable(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("v")
            if len(calls) == 2:
                raise KeyError("k")
            return "done"

        result = retry_sync(
            fn,
            max_attempts=5,
            base_delay=0,
            jitter=0,
            retryable=(ValueError, KeyError),
        )
        self.assertEqual(result, "done")
        self.assertEqual(len(calls), 3)


class CircuitBreakerTests(unittest.TestCase):
    def _make_cb(self, threshold=3, recovery=0.05):
        return CircuitBreaker(
            failure_threshold=threshold,
            recovery_seconds=recovery,
            name="test",
        )

    def test_starts_closed(self):
        cb = self._make_cb()
        self.assertEqual(cb.state, CircuitBreaker.CLOSED)

    def test_allow_request_when_closed(self):
        cb = self._make_cb()
        self.assertTrue(cb.allow_request())

    def test_opens_after_threshold_failures(self):
        cb = self._make_cb(threshold=3)
        cb.record_failure()
        cb.record_failure()
        self.assertEqual(cb.state, CircuitBreaker.CLOSED)
        cb.record_failure()
        self.assertEqual(cb.state, CircuitBreaker.OPEN)

    def test_rejects_when_open(self):
        cb = self._make_cb(threshold=1)
        cb.record_failure()
        self.assertFalse(cb.allow_request())

    def test_success_resets_to_closed(self):
        cb = self._make_cb(threshold=1)
        cb.record_failure()
        self.assertEqual(cb.state, CircuitBreaker.OPEN)
        cb.record_success()
        self.assertEqual(cb.state, CircuitBreaker.CLOSED)
        self.assertTrue(cb.allow_request())

    def test_transitions_to_half_open_after_recovery(self):
        cb = self._make_cb(threshold=1, recovery=0.01)
        cb.record_failure()
        self.assertEqual(cb.state, CircuitBreaker.OPEN)
        time.sleep(0.05)
        # State is computed lazily on property access.
        self.assertEqual(cb.state, CircuitBreaker.HALF_OPEN)
        self.assertTrue(cb.allow_request())

    def test_half_open_success_closes_circuit(self):
        cb = self._make_cb(threshold=1, recovery=0.01)
        cb.record_failure()
        time.sleep(0.05)
        cb.allow_request()  # trigger half-open probe
        cb.record_success()
        self.assertEqual(cb.state, CircuitBreaker.CLOSED)

    def test_half_open_failure_reopens_circuit(self):
        cb = self._make_cb(threshold=1, recovery=0.01)
        cb.record_failure()
        time.sleep(0.05)
        cb.allow_request()
        cb.record_failure()
        self.assertEqual(cb.state, CircuitBreaker.OPEN)

    def test_snapshot_returns_expected_keys(self):
        cb = self._make_cb()
        snap = cb.snapshot()
        self.assertIn("state", snap)
        self.assertIn("failures", snap)
        self.assertIn("threshold", snap)
        self.assertIn("recovery_seconds", snap)

    def test_thread_safe_concurrent_failures(self):
        cb = CircuitBreaker(failure_threshold=100, recovery_seconds=60, name="mt")
        errors: list[Exception] = []

        def record_many():
            try:
                for _ in range(25):
                    cb.record_failure()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=record_many) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(cb._failures, 100)

    def test_circuit_open_error_is_runtime_error(self):
        self.assertTrue(issubclass(CircuitOpenError, RuntimeError))


if __name__ == "__main__":
    unittest.main()
