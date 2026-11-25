"""Tests for disk priority: background reads on slow drives wait for the viewer."""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reviewer_imaging as ri  # noqa: E402


class DiskGateTests(unittest.TestCase):
    def test_system_drive_never_waits(self):
        p = os.path.join(os.environ.get("SystemDrive", "C:") + "\\", "x.NEF")
        self.assertFalse(ri.slow_drive(p))
        with ri.foreground_read():
            t0 = time.monotonic()
            with ri.background_read(p):
                pass
            self.assertLess(time.monotonic() - t0, 0.05)

    def test_other_drives_and_shares_are_slow(self):
        other = "Q:" if ri._SYSTEM_DRIVE != "Q:" else "R:"
        self.assertTrue(ri.slow_drive(other + r"\Photos\x.NEF"))
        self.assertTrue(ri.slow_drive(r"\\nas\photos\x.NEF"))

    def test_background_waits_for_the_viewer(self):
        with mock.patch.object(ri, "slow_drive", lambda p: True):
            started = []

            def bg():
                with ri.background_read("Q:\\x.NEF"):
                    started.append(time.monotonic())

            with ri.foreground_read():
                t = threading.Thread(target=bg)
                t.start()
                time.sleep(0.3)
                self.assertEqual(started, [])          # held back while the viewer reads
                released = time.monotonic()
            t.join(5)
            self.assertTrue(started)
            self.assertGreaterEqual(started[0] - released, ri._FG_GRACE - 0.05)

    def test_one_background_read_at_a_time_per_drive(self):
        with mock.patch.object(ri, "slow_drive", lambda p: True), \
                mock.patch.object(ri, "_fg_last", 0.0):
            inside, peak = [0], [0]
            lock = threading.Lock()

            def bg():
                with ri.background_read("Q:\\x.NEF"):
                    with lock:
                        inside[0] += 1
                        peak[0] = max(peak[0], inside[0])
                    time.sleep(0.05)
                    with lock:
                        inside[0] -= 1

            ts = [threading.Thread(target=bg) for _ in range(4)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(5)
            self.assertEqual(peak[0], 1)

    def test_nested_background_reads_do_not_deadlock(self):
        with mock.patch.object(ri, "slow_drive", lambda p: True), \
                mock.patch.object(ri, "_fg_last", 0.0):
            done = []

            def bg():
                with ri.background_read("Q:\\x.NEF"):
                    with ri.background_read("Q:\\y.NEF"):
                        done.append(1)

            t = threading.Thread(target=bg)
            t.start()
            t.join(3)
            self.assertEqual(done, [1])

    def test_chunked_read_returns_the_whole_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.bin")
            data = os.urandom(300_000)
            Path(p).write_bytes(data)
            with mock.patch.object(ri, "slow_drive", lambda p: True), \
                    mock.patch.object(ri, "_fg_last", 0.0):
                self.assertEqual(ri.read_file_background(p, chunk=65536), data)

    def test_hold_pauses_background_until_released(self):
        with mock.patch.object(ri, "slow_drive", lambda p: True), \
                mock.patch.object(ri, "_fg_last", 0.0):
            started = []

            def bg():
                with ri.background_read(r"Q:\x.NEF", max_wait=0.2):
                    started.append(time.monotonic())

            with ri.hold_background():
                t = threading.Thread(target=bg)
                t.start()
                time.sleep(0.6)                         # longer than max_wait: still held
                self.assertEqual(started, [])
            t.join(5)
            self.assertTrue(started)


if __name__ == "__main__":
    unittest.main()
