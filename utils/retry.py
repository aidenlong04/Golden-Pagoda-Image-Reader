"""Retry utilities with exponential back-off, jitter, and circuit breaker.

Used by ocr_engine.py (OCR.space API retries) and bot.py (Discord role
operations). All primitives here are synchronous and thread-safe so they
run cleanly inside worker threads spawned by asyncio.to_thread / _run_heavy.

Tunable via env vars (read once at import time):
  OCR_RETRY_MAX_ATTEMPTS    default 3
  OCR_RETRY_BASE_DELAY      default 1.0  (seconds)
  OCR_RETRY_MAX_DELAY       default 30.0 (seconds)
  OCR_CIRCUIT_BREAKER_THRESHOLD  default 5  (consecutive failures before open)
  OCR_CIRCUIT_BREAKER_RECOVERY   default 60 (seconds before half-open probe)
"""
from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from typing import TypeVar

from config import _float_env, _int_env

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Back-off helpers
# ---------------------------------------------------------------------------

def exponential_backoff(
    attempt: int,
    *,
    base: float = 1.0,
    cap: float = 30.0,
    jitter: float = 0.25,
) -> float:
    """Return the sleep duration for *attempt* (1-based) using full-jitter
    exponential back-off capped at *cap* seconds.

    Strategy: compute ``interval = min(cap, base * 2^(attempt-1))``, then
    multiply by a random factor in ``[1 - jitter, 1 + jitter]`` (clamped to
    ``[0, cap]``).  A jitter of 0.25 means ±25 % around the deterministic
    interval, which prevents thundering herds when many workers retry
    simultaneously.
    """
    interval = min(cap, base * (2 ** min(attempt - 1, 30)))
    factor = 1.0 + random.uniform(-jitter, jitter)
    return max(0.0, min(cap, interval * factor))


def retry_sync(
    func: Callable[..., T],
    /,
    *args,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: float = 0.25,
    retryable: type[Exception] | tuple[type[Exception], ...] = Exception,
    on_retry: Callable[[int, Exception, float], None] | None = None,
    **kwargs,
) -> T:
    """Call ``func(*args, **kwargs)`` with exponential-backoff retries.

    Parameters
    ----------
    func:
        Callable to invoke.
    max_attempts:
        Total number of attempts (including the first one).
    base_delay, max_delay, jitter:
        Forwarded to :func:`exponential_backoff`.
    retryable:
        Exception type(s) that trigger a retry; any other exception
        propagates immediately without consuming a retry slot.
    on_retry:
        Optional ``(attempt, exc, sleep_secs) -> None`` hook — called
        just before sleeping so callers can log/metric retries.
    """
    last: Exception
    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except retryable as exc:  # type: ignore[misc]
            last = exc
            if attempt >= max_attempts:
                break
            delay = exponential_backoff(
                attempt, base=base_delay, cap=max_delay, jitter=jitter
            )
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            time.sleep(delay)
    raise last  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

class CircuitOpenError(RuntimeError):
    """Raised when a call is rejected because the circuit is open."""


class CircuitBreaker:
    """Three-state circuit breaker: CLOSED → OPEN → HALF_OPEN → CLOSED.

    Thread-safe via an internal ``threading.Lock``.  Designed for the
    OCR.space API: tracks consecutive failures and, once
    *failure_threshold* is exceeded, rejects calls immediately by raising
    :exc:`CircuitOpenError` until *recovery_seconds* elapses.  After the
    recovery window the breaker enters HALF_OPEN and allows one probe
    request; success closes the circuit, failure re-opens it.

    Parameters
    ----------
    failure_threshold:
        Number of consecutive failures that trip the circuit open.
    recovery_seconds:
        How long to stay OPEN before allowing a probe (HALF_OPEN).
    half_open_max:
        Maximum simultaneous in-flight probes in HALF_OPEN state.
    name:
        Human-readable label used in log messages.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_seconds: float = 60.0,
        half_open_max: int = 1,
        name: str = "circuit",
    ) -> None:
        self._lock = threading.Lock()
        self._state = self.CLOSED
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open_in_flight = 0
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.half_open_max = half_open_max
        self.name = name

    @property
    def state(self) -> str:
        with self._lock:
            return self._current_state()

    def _current_state(self) -> str:
        """Must be called with ``self._lock`` held."""
        if self._state == self.OPEN and self._opened_at is not None:
            if time.monotonic() - self._opened_at >= self.recovery_seconds:
                self._state = self.HALF_OPEN
                self._half_open_in_flight = 0
                logger.info("CircuitBreaker[%s]: OPEN → HALF_OPEN", self.name)
        return self._state

    def allow_request(self) -> bool:
        """Return True if the call may proceed; False if the circuit is OPEN
        and the recovery window has not elapsed yet.

        Does *not* raise — callers decide whether to raise
        :exc:`CircuitOpenError` or silently skip.
        """
        with self._lock:
            state = self._current_state()
            if state == self.CLOSED:
                return True
            if state == self.OPEN:
                return False
            # HALF_OPEN: allow up to half_open_max concurrent probes.
            if self._half_open_in_flight < self.half_open_max:
                self._half_open_in_flight += 1
                return True
            return False

    def record_success(self) -> None:
        """Reset failure count and close the circuit."""
        with self._lock:
            prev = self._state
            self._failures = 0
            self._state = self.CLOSED
            self._opened_at = None
            self._half_open_in_flight = 0
            if prev != self.CLOSED:
                logger.info("CircuitBreaker[%s]: %s → CLOSED", self.name, prev.upper())

    def record_failure(self) -> None:
        """Increment the failure counter and open the circuit if the
        threshold is reached."""
        with self._lock:
            self._failures += 1
            if self._state == self.HALF_OPEN:
                self._state = self.OPEN
                self._opened_at = time.monotonic()
                self._half_open_in_flight = 0
                logger.warning(
                    "CircuitBreaker[%s]: HALF_OPEN → OPEN (probe failed)", self.name
                )
            elif self._state == self.CLOSED and self._failures >= self.failure_threshold:
                self._state = self.OPEN
                self._opened_at = time.monotonic()
                logger.warning(
                    "CircuitBreaker[%s]: CLOSED → OPEN (%d failures)",
                    self.name,
                    self._failures,
                )

    def snapshot(self) -> dict:
        """Return a JSON-serialisable snapshot for the /status page."""
        with self._lock:
            return {
                "state": self._current_state(),
                "failures": self._failures,
                "threshold": self.failure_threshold,
                "recovery_seconds": self.recovery_seconds,
            }


# ---------------------------------------------------------------------------
# Module-level OCR circuit-breaker instance (shared by ocr_engine.py)
# ---------------------------------------------------------------------------

# Tunable defaults read once at import time.
OCR_RETRY_MAX_ATTEMPTS: int = _int_env("OCR_RETRY_MAX_ATTEMPTS", 3)
OCR_RETRY_BASE_DELAY: float = _float_env("OCR_RETRY_BASE_DELAY", 1.0)
OCR_RETRY_MAX_DELAY: float = _float_env("OCR_RETRY_MAX_DELAY", 30.0)

ocr_circuit_breaker: CircuitBreaker = CircuitBreaker(
    failure_threshold=_int_env("OCR_CIRCUIT_BREAKER_THRESHOLD", 5),
    recovery_seconds=_float_env("OCR_CIRCUIT_BREAKER_RECOVERY", 60.0),
    name="ocr.space",
)
