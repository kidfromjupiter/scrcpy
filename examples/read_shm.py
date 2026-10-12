"""Display/process the newest phone frame; --no-display measures IPC age."""

import argparse
import math
import time

import cv2
import numpy as np
from shm_reader import SharedMemoryReader


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", nargs="?", default="phone")
    parser.add_argument("--format", choices=("bgr", "gray"), default="bgr")
    parser.add_argument("--no-display", action="store_true")
    parser.add_argument("--duration", type=float, default=0,
                        help="Stop after this many seconds (0: until disconnected)")
    args = parser.parse_args()
    if not math.isfinite(args.duration) or args.duration < 0:
        parser.error("--duration must be finite and nonnegative")
    ages, skipped = [], 0
    producer_skipped = 0
    conversion_and_read = []
    last_sequence = None
    start = time.monotonic()
    try:
        with SharedMemoryReader(args.name) as reader:
            while not args.duration or time.monotonic() - start < args.duration:
                before = time.perf_counter()
                frame = reader.read(timeout=0, format=args.format)
                elapsed = (time.perf_counter() - before) * 1000
                if frame is None:
                    # Block on an event instead of busy-polling. This sample's
                    # timings include waiting; the age field always excludes it.
                    frame = reader.read(timeout=1, format=args.format)
                    elapsed = None
                if frame is None:
                    continue
                if last_sequence is not None:
                    skipped += frame.sequence - last_sequence - 1
                last_sequence = frame.sequence
                producer_skipped = frame.producer_skipped
                ages.append(frame.publication_age_ms)
                if elapsed is not None:
                    conversion_and_read.append(elapsed)
                # Apply your OpenCV processing to frame.image here.
                if not args.no_display:
                    cv2.imshow("scrcpy shared memory", frame.image)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        break
    except (EOFError, KeyboardInterrupt) as error:
        print(error)
    finally:
        cv2.destroyAllWindows()
    if ages:
        print(f"Frames: {len(ages)}; reader skipped: {skipped}; "
              f"producer contention skips: {producer_skipped}")
        print(f"Publication-to-copy age: median {np.median(ages):.3f} ms; "
              f"p95 {np.percentile(ages, 95):.3f} ms")
        if conversion_and_read:
            print(f"Nonblocking copy + conversion: median "
                  f"{np.median(conversion_and_read):.3f} ms")
        print("These measurements exclude camera capture, USB transport and decoding.")


if __name__ == "__main__":
    main()
