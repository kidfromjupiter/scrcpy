"""Cross-process tests against the real C sink, without an Android device.

Run with the project's virtualenv after a Windows debug build:
    python examples/tests/test_shm_reader.py
The C fixture path can be overridden with SCRCPY_SHM_FIXTURE.
"""

import ctypes as C
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import cv2
from shm_reader import SharedMemoryReader, HEADER_SIZE

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(os.environ.get("SCRCPY_SHM_FIXTURE",
                             ROOT / "build-win64-tests/app/test_shm_sink.exe"))
ENV = os.environ.copy()
ENV["PATH"] = str(ROOT / "dist/scrcpy-win64-v4.1-custom") + os.pathsep + ENV["PATH"]


class SharedMemoryTests(unittest.TestCase):
    def setUp(self):
        self.name = "ipc-" + uuid.uuid4().hex
        self.writer = subprocess.Popen([str(FIXTURE), "--serve", self.name],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, env=ENV)
        self.assertEqual(self.writer.stdout.readline().strip(), "READY")
        self.reader = SharedMemoryReader(self.name)

    def command(self, text):
        self.writer.stdin.write(text + "\n")
        self.writer.stdin.flush()
        return self.writer.stdout.readline().strip()

    def tearDown(self):
        self.reader.close()
        if self.writer.poll() is None:
            self.writer.stdin.write("quit\n")
            self.writer.stdin.flush()
            try:
                self.writer.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.writer.kill()
                self.writer.wait(timeout=5)
        for pipe in (self.writer.stdin, self.writer.stdout, self.writer.stderr):
            pipe.close()

    @staticmethod
    def expected(w, h, value, plane):
        return ((np.arange(h)[:, None] + np.arange(w)[None, :]
                 + value + plane * 64) % 256).astype(np.uint8)

    def test_padding_negative_stride_resize_and_owned_pixels(self):
        self.command("publish 4 4 10 0")
        first = self.reader.read(format="yuv")
        self.assertEqual(first.sequence, 1)
        self.assertEqual(first.pts, 10000)
        self.assertEqual((first.color_range, first.color_space), (1, 1))
        for p, image in enumerate(first.image):
            np.testing.assert_array_equal(image, self.expected(4 if not p else 2,
                                                              4 if not p else 2, 10, p))
        self.command("publish 6 4 20 1")
        second = self.reader.read(format="yuv")
        self.assertEqual((second.width, second.height), (6, 4))
        for p, image in enumerate(second.image):
            np.testing.assert_array_equal(image, self.expected(6 if not p else 3,
                                                              4 if not p else 2, 20, p))
        np.testing.assert_array_equal(first.image[0], self.expected(4, 4, 10, 0))
        self.assertTrue(first.image[0].flags.writeable)

    def test_bgr_and_gray_conversion(self):
        self.command("publish 6 4 60 0")
        frame = self.reader.read(format="bgr")
        planes = [self.expected(6 if not p else 3, 4 if not p else 2, 60, p)
                  for p in range(3)]
        i420 = np.concatenate([p.ravel() for p in planes]).reshape(6, 6)
        np.testing.assert_array_equal(frame.image, cv2.cvtColor(i420, cv2.COLOR_YUV2BGR_I420))
        self.command("publish 6 4 61 0")
        gray = self.reader.read(format="gray")
        np.testing.assert_array_equal(gray.image, self.expected(6, 4, 61, 0))

    def test_odd_dimensions(self):
        self.command("publish 5 3 1 0")
        frame = self.reader.read(format="yuv")
        self.assertEqual([p.shape for p in frame.image], [(3, 5), (2, 3), (2, 3)])
        self.command("publish 5 3 2 1")
        with self.assertRaisesRegex(ValueError, "even dimensions"):
            self.reader.read(format="bgr")
        self.command("publish 5 3 3 0")
        self.assertEqual(self.reader.read(format="gray").image.shape, (3, 5))

    def test_full_range_camera_format(self):
        self.assertEqual(self.command("publishj 6 4 10 1"), "PUBLISHED 1")
        frame = self.reader.read(format="yuv")
        self.assertEqual(frame.color_range, 2)
        for p, image in enumerate(frame.image):
            np.testing.assert_array_equal(image, self.expected(6 if not p else 3,
                                                              4 if not p else 2, 10, p))

    def test_latest_frame_and_timeout(self):
        self.assertIsNone(self.reader.read(timeout=0, format="gray"))
        for i in range(12):
            self.command(f"publish 4 4 {i} 0")
        frame = self.reader.read(timeout=0, format="gray")
        self.assertEqual(frame.sequence, 12)
        self.assertEqual(int(frame.image[0, 0]), 11)
        self.assertIsNone(self.reader.read(timeout=0.01, format="gray"))

    def test_reader_lock_skips_without_blocking_producer(self):
        self.assertTrue(self.reader._lock(1000))
        try:
            self.assertEqual(self.command("publish 4 4 1 0"), "PUBLISHED 0")
        finally:
            self.reader._api.ReleaseMutex(self.reader._handles["mutex"])
        self.command("publish 4 4 2 0")
        frame = self.reader.read(format="gray")
        self.assertEqual(frame.sequence, 1)
        self.assertEqual(frame.producer_skipped, 1)

    def test_collision_and_single_reader(self):
        with self.assertRaisesRegex(RuntimeError, "Another reader"):
            SharedMemoryReader(self.name)
        duplicate = subprocess.run([str(FIXTURE), "--serve", self.name],
                                   capture_output=True, text=True, env=ENV, timeout=5)
        self.assertEqual(duplicate.returncode, 2)
        self.command("publish 4 4 1 0")
        self.assertIsNotNone(self.reader.read(format="gray"))

    def test_shutdown_and_restart(self):
        self.assertEqual(self.command("close"), "CLOSED")
        with self.assertRaises(EOFError):
            self.reader.read(timeout=0.1)
        self.reader.close()
        self.command("quit")
        self.writer.wait(timeout=5)
        replacement = subprocess.Popen([str(FIXTURE), "--serve", self.name],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, env=ENV)
        try:
            self.assertEqual(replacement.stdout.readline().strip(), "READY")
            with SharedMemoryReader(self.name) as reader:
                replacement.stdin.write("publish 4 4 2 0\n")
                replacement.stdin.flush()
                self.assertEqual(reader.read(format="gray").sequence, 1)
        finally:
            replacement.communicate("quit\n", timeout=5)

    def test_crash(self):
        self.writer.kill()
        self.writer.wait(timeout=5)
        with self.assertRaises(EOFError):
            self.reader.read(timeout=0.1)

    def test_abandoned_mutex(self):
        code = ("import sys,os; sys.path.insert(0,sys.argv[1]); "
                "from shm_reader import _windows_api; a=_windows_api(); "
                "h=a.OpenMutexW(0x100001,False,sys.argv[2]); "
                "assert a.WaitForSingleObject(h,1000)==0; os._exit(0)")
        subprocess.run([sys.executable, "-c", code, str(ROOT / "examples"),
                        "Local\\scrcpy.shm." + self.name + ".mutex"],
                       check=True, env=ENV, timeout=10)
        with self.assertRaisesRegex(EOFError, "abandoned"):
            self.reader.read(timeout=0.1)

    def test_corrupt_layout_rejected(self):
        self.command("publish 4 4 1 0")
        # The fixture owns writable memory. Open a temporary write view to inject
        # malformed metadata; reader itself maps read-only and validates bounds.
        api = self.reader._api
        mapping = api.OpenFileMappingW(2, False, self.reader._prefix + ".frame")
        view = api.MapViewOfFile(mapping, 2, 0, 0, HEADER_SIZE)
        self.assertTrue(view)
        try:
            self.assertTrue(self.reader._lock(1000))
            try:
                C.c_uint32.from_address(view + 40).value = 0xFFFFFFFF
            finally:
                api.ReleaseMutex(self.reader._handles["mutex"])
            with self.assertRaisesRegex(ValueError, "layout"):
                self.reader.read(format="yuv")
        finally:
            api.UnmapViewOfFile(view)
            api.CloseHandle(mapping)

    def test_stress_no_torn_frames(self):
        self.assertEqual(self.command("stream 160 96 1000 1"), "STREAM")
        frames = []
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            frame = self.reader.read(timeout=0.01, format="yuv")
            if frame is None:
                continue
            value = int(frame.image[0][0, 0])
            for p, image in enumerate(frame.image):
                np.testing.assert_array_equal(image, self.expected(160 if not p else 80,
                                                                  96 if not p else 48, value, p))
            frames.append(frame.sequence)
            time.sleep(0.04)  # Slow processing deliberately skips intermediate frames.
        self.assertGreater(len(frames), 10)
        self.assertEqual(frames, sorted(set(frames)))
        self.assertGreater(frames[-1], len(frames))
        self.assertEqual(self.writer.stdout.readline().strip(), "DONE")

    def test_publication_age_benchmark(self):
        self.assertEqual(self.command("stream 1280 720 120 16"), "STREAM")
        ages, conversion_times = [], []
        deadline = time.monotonic() + 6
        for _ in range(120):
            if time.monotonic() >= deadline:
                break
            frame = self.reader.read(timeout=0.05, format="yuv")
            if frame is None:
                break
            ages.append(frame.publication_age_ms)
            packed = np.concatenate([p.ravel() for p in frame.image]).reshape(1080, 1280)
            before = time.perf_counter()
            cv2.cvtColor(packed, cv2.COLOR_YUV2BGR_I420)
            conversion_times.append((time.perf_counter() - before) * 1000)
        self.assertGreater(len(ages), 30)
        self.assertTrue(all(age >= 0 for age in ages))
        self.assertEqual(self.writer.stdout.readline().strip(), "DONE")
        print(f"\n720p synthetic IPC: frames={len(ages)}, "
              f"age median={np.median(ages):.3f} ms, p95={np.percentile(ages,95):.3f} ms; "
              f"conversion median={np.median(conversion_times):.3f} ms", flush=True)

    def test_input_validation_and_missing_producer(self):
        for name in ("", "a/b", "a b", "a" * 65):
            with self.assertRaises(ValueError):
                SharedMemoryReader(name)
        with self.assertRaises(TimeoutError):
            SharedMemoryReader("missing-" + uuid.uuid4().hex, attach_timeout=0.02)
        # Missing attachment must not leave a mapping/consumer reservation.
        with self.assertRaises(TimeoutError):
            SharedMemoryReader("missing-" + uuid.uuid4().hex, attach_timeout=0)
        for timeout in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.reader.read(timeout=timeout)

    def test_late_attachment(self):
        self.reader.close()
        for value in range(3):
            self.command(f"publish 4 4 {value} 0")
        self.reader = SharedMemoryReader(self.name)
        frame = self.reader.read(format="gray")
        self.assertEqual(frame.sequence, 3)
        self.assertEqual(int(frame.image[0, 0]), 2)

    def test_reader_crash_releases_handles(self):
        self.reader.close()
        code = ("import sys,os; sys.path.insert(0,sys.argv[1]); "
                "from shm_reader import SharedMemoryReader; "
                "r=SharedMemoryReader(sys.argv[2]); os._exit(0)")
        subprocess.run([sys.executable, "-c", code, str(ROOT / "examples"), self.name],
                       check=True, env=ENV, timeout=10)
        self.reader = SharedMemoryReader(self.name)
        self.command("publish 4 4 10 0")
        self.assertEqual(self.reader.read(format="gray").sequence, 1)

    def test_capture_example_with_stalled_producer(self):
        self.reader.close()
        self.command("publish 4 4 10 0")
        sample = subprocess.run([sys.executable, str(ROOT / "examples/read_shm.py"),
                                 self.name, "--no-display", "--duration=0.1"],
                                capture_output=True, text=True, env=ENV, timeout=5)
        self.assertEqual(sample.returncode, 0, sample.stderr)
        self.assertIn("Frames: 1", sample.stdout)
        self.assertIn("Publication-to-copy age", sample.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
