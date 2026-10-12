# Windows shared-memory output

Send decoded video directly to Python/OpenCV, bypassing window capture and OBS.
Windows 64-bit is the tested target. The Android server is unchanged.

```powershell
# From the portable package directory (camera must support this size/rate):
.\scrcpy.exe --video-source=camera --camera-facing=back --camera-size=1280x720 --camera-fps=60 --no-audio --no-window --shm-sink=phone
```

`--shm-sink` also works with display mirroring, preview and recording. It receives
decoded frames before playback buffering; `--video-buffer` does not delay it.
It requires video and is incompatible with OTG mode. Names contain 1–64 ASCII
letters, digits, underscores, dots or hyphens. Each name permits one producer
and one Python reader in the same Windows login session. Use different names
for multiple devices. This does not create a Windows webcam device.

## Python usage

Install dependencies in a local environment, then run the bundled example:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install numpy opencv-python
.\.venv\Scripts\python.exe examples\read_shm.py phone
# No preview, 10-second publication-age measurement:
.\.venv\Scripts\python.exe examples\read_shm.py phone --no-display --duration=10
```

Put `examples/shm_reader.py` beside your own script or add `examples` to your
Python import path:

```python
from shm_reader import SharedMemoryReader

with SharedMemoryReader("phone", attach_timeout=5) as reader:
    while True:
        try:
            frame = reader.read(timeout=1, format="bgr")
        except EOFError:
            break
        if frame is None:
            continue
        image = frame.image  # Owned uint8 NumPy array, H × W × 3, BGR
        # Process image here; intermediate frames are skipped automatically.
```

`format="gray"` copies just the Y plane (raw luma, without range expansion).
`format="yuv"` returns `(Y, U, V)` arrays; chroma dimensions round up for odd
widths/heights. BGR uses OpenCV's I420 conversion and requires even dimensions.
OpenCV assumes limited-range BT.601; streams marked full-range or BT.709 may
need your own conversion. FFmpeg color-range/space enum values are exposed as
metadata. No conversion is performed by scrcpy.

`read()` returns the newest frame not previously read, or `None` on timeout.
It raises `EOFError` on producer shutdown, crash or mutex abandonment. Close the
reader before restarting scrcpy with the same name. Handles held by a previous
reader keep Windows objects alive; name reuse intentionally fails instead of
overwriting stale memory. The reader is intended for a single calling thread.
Attachment does not create a mapping when no producer exists.

Pixels and metadata are copied under a named mutex. scrcpy never waits for that
mutex: it skips a frame if busy. Python releases the mutex before conversion or
processing; returned arrays own their memory and remain valid across later
frames. Notification events coalesce rather than queuing frames. Frame
`sequence` counts published frames; `producer_skipped` counts mutex contention
skips. Neither is a count of frames lost elsewhere in the pipeline.

## Latency

Prefer USB, no preview/audio, a supported 60 fps camera mode, and grayscale if
your processing does not need color. Python naturally gets the newest available
frame after finishing its previous processing. This removes OBS scheduling and
capture overhead but cannot remove camera, encoding, transport or decoding time.

`publication_age_ms` measures time from publishing the copied YUV frame until
the Python copy finishes, using Windows QPC on both processes. It excludes
subsequent BGR conversion and is **not** end-to-end camera latency. Frame `pts`
is the source presentation timestamp (microseconds, or FFmpeg's unknown sentinel
`-9223372036854775808`); do not compare it directly to Windows QPC.

Measure camera-to-application latency separately by filming a timer and comparing
it with the OpenCV preview. Compare configurations on the same phone and host;
no fixed millisecond latency is guaranteed.

## Protocol v1

Objects are `Local\scrcpy.shm.NAME.frame`, `.mutex`, and `.event`. The mapping
contains a 128-byte little-endian header followed by a 64 MiB payload capacity.
The Python reader reserves `.reader` to reject simultaneous consumers. Objects
use default Windows security and are not available across login sessions.

| Offset | Type | Field |
| --- | --- | --- |
| 0 | 8 bytes | Magic `SCRCSHM1` |
| 8, 12 | uint32 | Version (1), header size (128) |
| 16, 20 | uint32 | Payload capacity (67108864), producer PID |
| 24, 28 | uint32 | Status (0 initializing, 1 live, 2 closed), format (1 I420) |
| 32 | uint64 | Published sequence, initially 0 |
| 40, 44 | uint32 | Width, height |
| 48 | 3 × uint32 | Y/U/V offsets relative to payload |
| 60 | 3 × uint32 | Y/U/V lengths |
| 72, 76 | uint32 | FFmpeg color range, color space |
| 80 | int64 | Frame PTS |
| 88, 96 | int64 | Publication QPC counter, QPC frequency |
| 104 | uint64 | Producer contention skips |
| 112 | 16 bytes | Reserved, zero |

Planes are contiguous and tightly packed: `Y = width × height`,
`U = V = ceil(width/2) × ceil(height/2)`. Dimensions can change between frames.
Both YUV420P and full-range YUVJ420P decoder frames use this plane layout;
YUVJ420P is exported with color range 2 (JPEG/full range). Other pixel formats
or payloads exceeding capacity fail with the actual format and a clear error.

Hold the mutex while accessing fields and pixels. Status is independently
updated with Windows `InterlockedExchange` so shutdown never waits for a reader.
The producer uses a zero-time mutex wait, updates the complete frame and sequence,
releases the mutex, then signals an auto-reset event. Readers check sequence
before waiting and wait on both the event and producer process handle, preventing
stale event notifications or producer crashes from causing indefinite hangs.
