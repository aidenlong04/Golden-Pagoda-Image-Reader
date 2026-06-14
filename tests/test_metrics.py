"""Tests for utils.metrics — SemaphoreMetrics, LatencyRecorder, metrics_snapshot."""
from __future__ import annotations

import threading
import time
import unittest

from utils.metrics import LatencyRecorder, SemaphoreMetrics, metrics_snapshot


class SemaphoreMetricsTests(unittest.TestCase):
    def setUp(self):
        self.m = SemaphoreMetrics()

    def test_initial_snapshot_is_zero(self):
        s = self.m.snapshot()
        self.assertEqual(s["current"], 0)
        self.assertEqual(s["queued"], 0)
        self.assertEqual(s["peak"], 0)
        self.assertEqual(s["total_acquired"], 0)
        self.assertEqual(s["avg_queue_wait_ms"], 0.0)

    def test_enqueue_increments_queued(self):
        self.m.record_enqueue()
        self.assertEqual(self.m.snapshot()["queued"], 1)

    def test_acquire_decrements_queued_and_increments_current(self):
        ts = self.m.record_enqueue()
        self.m.record_acquire(ts)
        s = self.m.snapshot()
        self.assertEqual(s["queued"], 0)
        self.assertEqual(s["current"], 1)
        self.assertEqual(s["total_acquired"], 1)

    def test_release_decrements_current(self):
        ts = self.m.record_enqueue()
        self.m.record_acquire(ts)
        self.m.record_release()
        self.assertEqual(self.m.snapshot()["current"], 0)

    def test_peak_tracks_maximum_current(self):
        ts1 = self.m.record_enqueue()
        ts2 = self.m.record_enqueue()
        ts3 = self.m.record_enqueue()
        self.m.record_acquire(ts1)
        self.m.record_acquire(ts2)
        self.m.record_acquire(ts3)
        self.m.record_release()
        self.assertEqual(self.m.snapshot()["peak"], 3)

    def test_peak_does_not_decrease_after_release(self):
        ts = self.m.record_enqueue()
        self.m.record_acquire(ts)
        self.m.record_release()
        # Peak should still be 1 after all slots are released.
        self.assertEqual(self.m.snapshot()["peak"], 1)

    def test_avg_queue_wait_computed(self):
        ts = self.m.record_enqueue()
        time.sleep(0.01)
        self.m.record_acquire(ts)
        s = self.m.snapshot()
        # avg_queue_wait_ms should reflect the ~10ms sleep.
        self.assertGreater(s["avg_queue_wait_ms"], 0.0)

    def test_thread_safe_concurrent_enqueue_acquire(self):
        errors: list[Exception] = []

        def worker():
            try:
                for _ in range(20):
                    ts = self.m.record_enqueue()
                    self.m.record_acquire(ts)
                    self.m.record_release()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        s = self.m.snapshot()
        self.assertEqual(s["total_acquired"], 100)

    def test_release_does_not_go_below_zero(self):
        # Extra release call should not crash or produce negative current.
        self.m.record_release()
        self.assertEqual(self.m.snapshot()["current"], 0)


class LatencyRecorderTests(unittest.TestCase):
    def setUp(self):
        self.r = LatencyRecorder(window=10)

    def test_empty_snapshot(self):
        s = self.r.snapshot()
        self.assertEqual(s["samples"], 0)
        self.assertEqual(s["p50_ms"], 0)
        self.assertEqual(s["p95_ms"], 0)
        self.assertEqual(s["p99_ms"], 0)

    def test_single_sample(self):
        self.r.record(100.0)
        s = self.r.snapshot()
        self.assertEqual(s["samples"], 1)
        self.assertAlmostEqual(s["p50_ms"], 100.0, places=1)
        self.assertAlmostEqual(s["p95_ms"], 100.0, places=1)
        self.assertAlmostEqual(s["p99_ms"], 100.0, places=1)

    def test_p50_p95_p99_ordering(self):
        for ms in range(1, 101):
            self.r.record(float(ms))
        s = self.r.snapshot()
        # Window is 10, so only last 10 values [91..100] are retained.
        self.assertEqual(s["samples"], 10)
        self.assertLessEqual(s["p50_ms"], s["p95_ms"])
        self.assertLessEqual(s["p95_ms"], s["p99_ms"])

    def test_avg_ms_correct(self):
        self.r.record(10.0)
        self.r.record(20.0)
        self.r.record(30.0)
        s = self.r.snapshot()
        self.assertAlmostEqual(s["avg_ms"], 20.0, places=1)

    def test_lru_eviction(self):
        # Window of 10: once full, oldest samples are evicted.
        for i in range(15):
            self.r.record(float(i))
        s = self.r.snapshot()
        self.assertEqual(s["samples"], 10)

    def test_thread_safe(self):
        errors: list[Exception] = []

        def record_many():
            try:
                for i in range(50):
                    self.r.record(float(i))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=record_many) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        s = self.r.snapshot()
        # Only window size (10) samples retained.
        self.assertEqual(s["samples"], 10)


class MetricsSnapshotTests(unittest.TestCase):
    def test_snapshot_returns_expected_keys(self):
        snap = metrics_snapshot()
        self.assertIn("semaphore", snap)
        self.assertIn("ocr_latency", snap)
        self.assertIn("analytics_latency", snap)

    def test_semaphore_snapshot_keys(self):
        sem = metrics_snapshot()["semaphore"]
        for key in ("current", "queued", "peak", "total_acquired", "avg_queue_wait_ms"):
            self.assertIn(key, sem)

    def test_latency_snapshot_keys(self):
        ocr = metrics_snapshot()["ocr_latency"]
        for key in ("samples", "avg_ms", "p50_ms", "p95_ms", "p99_ms"):
            self.assertIn(key, ocr)


if __name__ == "__main__":
    unittest.main()
