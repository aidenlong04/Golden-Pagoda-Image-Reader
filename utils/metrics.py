"""Lightweight in-process performance metrics for the Golden Pagoda bot.

Tracks the heavy-job semaphore utilisation, OCR/analytics query latency, and
provides a ``metrics_snapshot()`` entry-point that the /status command can
surface without adding external dependencies.

All operations are thread-safe.  Metrics are ephemeral (in-memory only) and
reset on each container start — use analytics.py for persistent history.

Module-level singletons are imported by bot.py and ocr_engine.py:

    from utils.metrics import heavy_semaphore_metrics, ocr_latency, metrics_snapshot

Tunable via env var:
  METRICS_LATENCY_WINDOW   default 500 (number of recent samples kept per recorder)
"""
from __future__ import annotations

import threading
import time
from collections import deque

from config import _int_env

_LATENCY_WINDOW: int = _int_env("METRICS_LATENCY_WINDOW", 500)


# ---------------------------------------------------------------------------
# Semaphore utilisation tracker
# ---------------------------------------------------------------------------

class SemaphoreMetrics:
    """Track current, peak, queued, and average-queue-wait for a semaphore.

    Intended usage pattern::

        enqueue_ts = metrics.record_enqueue()     # job starts waiting
        async with semaphore:
            metrics.record_acquire(enqueue_ts)    # got the lock
            try:
                ...
            finally:
                metrics.record_release()          # released the lock
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: int = 0
        self._peak: int = 0
        self._queued: int = 0
        self._total_acquired: int = 0
        self._total_queue_ms: float = 0.0

    def record_enqueue(self) -> float:
        """Mark a job as waiting for the semaphore.

        Returns the enqueue timestamp (monotonic) to pass to
        :meth:`record_acquire`.
        """
        with self._lock:
            self._queued += 1
        return time.monotonic()

    def record_acquire(self, enqueue_ts: float) -> None:
        """Mark the semaphore as acquired for the job that enqueued at
        *enqueue_ts*."""
        with self._lock:
            wait_ms = (time.monotonic() - enqueue_ts) * 1000.0
            self._queued = max(0, self._queued - 1)
            self._current += 1
            if self._current > self._peak:
                self._peak = self._current
            self._total_acquired += 1
            self._total_queue_ms += wait_ms

    def record_release(self) -> None:
        """Mark the semaphore as released."""
        with self._lock:
            self._current = max(0, self._current - 1)

    def snapshot(self) -> dict:
        """Return a JSON-serialisable snapshot."""
        with self._lock:
            avg_ms = (
                self._total_queue_ms / self._total_acquired
                if self._total_acquired
                else 0.0
            )
            return {
                "current": self._current,
                "queued": self._queued,
                "peak": self._peak,
                "total_acquired": self._total_acquired,
                "avg_queue_wait_ms": round(avg_ms, 1),
            }


# ---------------------------------------------------------------------------
# Latency recorder (rolling percentile window)
# ---------------------------------------------------------------------------

class LatencyRecorder:
    """Rolling window of recent latency samples for p50/p95/p99 percentiles.

    Parameters
    ----------
    window:
        Maximum number of recent samples to keep.  Older samples are
        evicted FIFO when the window fills.
    """

    def __init__(self, window: int = _LATENCY_WINDOW) -> None:
        self._lock = threading.Lock()
        self._samples: deque[float] = deque(maxlen=window)

    def record(self, latency_ms: float) -> None:
        with self._lock:
            self._samples.append(latency_ms)

    def _percentile(self, sorted_samples: list[float], pct: float) -> float:
        n = len(sorted_samples)
        if n == 0:
            return 0.0
        k = max(0, min(n - 1, round((pct / 100.0) * (n - 1))))
        return sorted_samples[k]

    def snapshot(self) -> dict:
        """Return a JSON-serialisable snapshot with common percentiles."""
        with self._lock:
            if not self._samples:
                return {
                    "samples": 0,
                    "avg_ms": 0,
                    "p50_ms": 0,
                    "p95_ms": 0,
                    "p99_ms": 0,
                }
            s = sorted(self._samples)
            n = len(s)
            return {
                "samples": n,
                "avg_ms": round(sum(s) / n, 1),
                "p50_ms": round(self._percentile(s, 50), 1),
                "p95_ms": round(self._percentile(s, 95), 1),
                "p99_ms": round(self._percentile(s, 99), 1),
            }


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

#: Tracks the ``_HEAVY_JOB_SEMAPHORE`` in bot.py.
heavy_semaphore_metrics: SemaphoreMetrics = SemaphoreMetrics()

#: OCR end-to-end latency (from _run_heavy enqueue to result, ms).
ocr_latency: LatencyRecorder = LatencyRecorder()

#: analytics.summary() query latency.
analytics_query_latency: LatencyRecorder = LatencyRecorder()


def metrics_snapshot() -> dict:
    """Return a combined snapshot of all tracked metrics.

    Safe to call from any thread at any time.
    """
    return {
        "semaphore": heavy_semaphore_metrics.snapshot(),
        "ocr_latency": ocr_latency.snapshot(),
        "analytics_latency": analytics_query_latency.snapshot(),
    }
