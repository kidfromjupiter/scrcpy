"""Windows scrcpy shared-memory protocol v1 reader (one reader per name).

Frames own their memory: keep/process them after read() without holding up scrcpy.
Only pixels copied under the mutex are returned; no unprotected shared views.
"""

from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
import math
import re
import struct
import sys
import time

import numpy as np

HEADER_SIZE = 128
CAPACITY = 64 * 1024 * 1024
HEADER = struct.Struct("<8s6IQ2I6I2I3qQ16s")
assert HEADER.size == HEADER_SIZE
WAIT_TIMEOUT = 258
WAIT_ABANDONED = 128
INFINITE = 0xFFFFFFFF
SYNCHRONIZE = 0x100000


def _windows_api():
    if sys.platform != "win32":
        raise OSError("scrcpy shared-memory output requires Windows")
    api = C.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateMutexW": ([C.c_void_p, W.BOOL, W.LPCWSTR], W.HANDLE),
        "OpenMutexW": ([W.DWORD, W.BOOL, W.LPCWSTR], W.HANDLE),
        "OpenEventW": ([W.DWORD, W.BOOL, W.LPCWSTR], W.HANDLE),
        "OpenFileMappingW": ([W.DWORD, W.BOOL, W.LPCWSTR], W.HANDLE),
        "MapViewOfFile": ([W.HANDLE, W.DWORD, W.DWORD, W.DWORD, C.c_size_t], C.c_void_p),
        "UnmapViewOfFile": ([C.c_void_p], W.BOOL),
        "OpenProcess": ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        "WaitForSingleObject": ([W.HANDLE, W.DWORD], W.DWORD),
        "WaitForMultipleObjects": ([W.DWORD, C.POINTER(W.HANDLE), W.BOOL, W.DWORD], W.DWORD),
        "ReleaseMutex": ([W.HANDLE], W.BOOL),
        "CloseHandle": ([W.HANDLE], W.BOOL),
        "QueryPerformanceCounter": ([C.POINTER(C.c_int64)], W.BOOL),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(api, name)
        fn.argtypes, fn.restype = args, result
    return api


@dataclass(frozen=True)
class Frame:
    image: np.ndarray | tuple[np.ndarray, np.ndarray, np.ndarray]
    sequence: int
    width: int
    height: int
    pts: int
    color_range: int
    color_space: int
    publication_age_ms: float
    producer_skipped: int
    format: str


class SharedMemoryReader:
    """Attach to --shm-sink=NAME. Use from one thread; close before restarting.

    read() returns the newest unconsumed frame, None on timeout, or raises
    EOFError on producer shutdown/crash. Publication age excludes camera,
    transport, decoding and the reader's subsequent color conversion.
    """

    def __init__(self, name: str, attach_timeout: float = 5.0):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
            raise ValueError("name must contain 1-64 ASCII letters/digits/_.-")
        self._validate_timeout(attach_timeout)
        self._api = _windows_api()
        self._prefix = "Local\\scrcpy.shm." + name
        self._handles = {}
        self._view = None
        self._sequence = 0
        self._closed = False
        try:
            # A named handle reserves one consumer, without thread ownership.
            consumer = self._api.CreateMutexW(None, False, self._prefix + ".reader")
            error = C.get_last_error()
            if not consumer:
                raise C.WinError(error)
            self._handles["consumer"] = consumer
            if error == 183:
                raise RuntimeError("Another reader is already attached to " + name)
            deadline = time.monotonic() + attach_timeout
            while not self._attach():
                self._detach()
                if time.monotonic() >= deadline:
                    raise TimeoutError("No active scrcpy shared-memory producer: " + name)
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _validate_timeout(timeout):
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be finite and nonnegative")

    def _lock(self, milliseconds):
        result = self._api.WaitForSingleObject(self._handles["mutex"], milliseconds)
        if result == 0:
            return True
        if result == WAIT_TIMEOUT:
            return False
        if result == WAIT_ABANDONED:
            self._api.ReleaseMutex(self._handles["mutex"])
            raise EOFError("Shared-memory mutex abandoned; close reader and restart scrcpy")
        raise C.WinError(C.get_last_error())

    def _attach(self):
        for key, suffix, fn, access in [
            ("mutex", ".mutex", self._api.OpenMutexW, SYNCHRONIZE | 1),
            ("event", ".event", self._api.OpenEventW, SYNCHRONIZE),
            ("mapping", ".frame", self._api.OpenFileMappingW, 4),
        ]:
            handle = fn(access, False, self._prefix + suffix)
            if not handle:
                error = C.get_last_error()
                if error == 2:
                    return False
                raise C.WinError(error)
            self._handles[key] = handle
        self._view = self._api.MapViewOfFile(self._handles["mapping"], 4, 0, 0,
                                            HEADER_SIZE + CAPACITY)
        if not self._view:
            raise C.WinError(C.get_last_error())
        if not self._lock(0):
            return False
        try:
            header = HEADER.unpack(C.string_at(self._view, HEADER_SIZE))
            if header[5] == 0:  # Header may still be initializing.
                return False
            self._validate_header(header)
            if header[5] == 2:
                raise EOFError("Shared-memory producer has stopped")
            pid = header[4]
        finally:
            self._api.ReleaseMutex(self._handles["mutex"])
        process = self._api.OpenProcess(SYNCHRONIZE, False, pid)
        if not process:
            if C.get_last_error() == 87:
                raise EOFError("Shared-memory producer exited")
            raise C.WinError(C.get_last_error())
        self._handles["process"] = process
        return True

    @staticmethod
    def _validate_header(h):
        if (h[0] != b"SCRCSHM1" or h[1] != 1 or h[2] != HEADER_SIZE
                or h[3] != CAPACITY or not h[4] or h[5] not in (0, 1, 2)
                or h[6] != 1 or h[20] <= 0):
            raise ValueError("Invalid or unsupported scrcpy shared-memory protocol")

    def read(self, timeout: float = 1.0, format: str = "bgr") -> Frame | None:
        self._validate_timeout(timeout)
        if format not in ("bgr", "gray", "yuv"):
            raise ValueError("format must be bgr, gray, or yuv")
        if self._closed:
            raise ValueError("Reader is closed")
        deadline = time.monotonic() + timeout
        while True:
            if self._api.WaitForSingleObject(self._handles["process"], 0) == 0:
                raise EOFError("Shared-memory producer exited")
            remaining = max(0, deadline - time.monotonic())
            milliseconds = min(INFINITE - 1, math.ceil(remaining * 1000))
            if self._lock(milliseconds):
                try:
                    h = HEADER.unpack(C.string_at(self._view, HEADER_SIZE))
                    self._validate_header(h)
                    if h[5] == 2:
                        raise EOFError("Shared-memory producer has stopped")
                    if h[7] > self._sequence:
                        w, height = h[8:10]
                        cw, ch = (w + 1) // 2, (height + 1) // 2
                        lengths = (w * height, cw * ch, cw * ch)
                        offsets = (0, lengths[0], lengths[0] + lengths[1])
                        total = sum(lengths)
                        if (not w or not height or total > CAPACITY
                                or tuple(h[10:13]) != offsets
                                or tuple(h[13:16]) != lengths):
                            raise ValueError("Invalid shared-memory frame layout")
                        # Copy only Y for grayscale; own the pixels before unlocking.
                        count = lengths[0] if format == "gray" else total
                        pixels = np.empty(count, dtype=np.uint8)
                        C.memmove(pixels.ctypes.data, self._view + HEADER_SIZE, count)
                        counter = C.c_int64()
                        self._api.QueryPerformanceCounter(C.byref(counter))
                        age = (counter.value - h[19]) * 1000 / h[20]
                        self._sequence = h[7]
                        break
                finally:
                    self._api.ReleaseMutex(self._handles["mutex"])
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            handles = (W.HANDLE * 2)(self._handles["process"], self._handles["event"])
            result = self._api.WaitForMultipleObjects(2, handles, False,
                            min(INFINITE - 1, math.ceil(remaining * 1000)))
            if result == 0:
                raise EOFError("Shared-memory producer exited")
            if result == WAIT_TIMEOUT:
                return None
            if result != 1:
                raise C.WinError(C.get_last_error())
        if format == "gray":
            image = pixels.reshape(height, w)
        elif format == "yuv":
            image = (pixels[:lengths[0]].reshape(height, w),
                     pixels[offsets[1]:offsets[2]].reshape(ch, cw),
                     pixels[offsets[2]:].reshape(ch, cw))
        else:
            if w % 2 or height % 2:
                raise ValueError("BGR conversion needs even dimensions; use --camera-size or format='yuv'")
            import cv2
            image = cv2.cvtColor(pixels.reshape(height * 3 // 2, w), cv2.COLOR_YUV2BGR_I420)
        return Frame(image, h[7], w, height, h[18], h[16], h[17], age, h[21], format)

    def _detach(self):
        if self._view:
            self._api.UnmapViewOfFile(self._view)
            self._view = None
        for key in list(self._handles):
            if key != "consumer":
                self._api.CloseHandle(self._handles.pop(key))

    def close(self):
        if not self._closed:
            self._detach()
            if "consumer" in self._handles:
                self._api.CloseHandle(self._handles.pop("consumer"))
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
