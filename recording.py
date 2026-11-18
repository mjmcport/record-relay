"""Bounded side recording from a supplied PCM stream, separate from playback."""
import argparse
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import wave

from capture import FRAME_BYTES, RATE, BLOCK_BYTES


def record_side(source, destination, maximum_seconds, *, enabled=False, before_publish=None):
    if not enabled:
        raise ValueError("Recording must be explicitly enabled")
    if destination.suffix.lower() != ".wav" or not 1 <= maximum_seconds <= 3600:
        raise ValueError("Use a .wav destination and limit of 1–3600 seconds")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    limit = maximum_seconds * RATE * FRAME_BYTES
    with TemporaryDirectory(dir=destination.parent, prefix=".record-relay-") as folder:
        candidate = Path(folder) / "side.wav"
        count = 0
        read_error = None
        with wave.open(str(candidate), "wb") as writer:
            writer.setparams((2, 2, RATE, 0, "NONE", "not compressed"))
            while count < limit:
                try:
                    block = source.read(min(BLOCK_BYTES, limit - count))
                except Exception as error:
                    read_error = error
                    break
                if not block:
                    break
                writer.writeframesraw(block)
                count += len(block)
        if read_error is not None:
            if count and count % FRAME_BYTES == 0:
                partial = destination.with_name(destination.stem + ".partial.wav")
                with candidate.open("rb") as source_file:
                    os.fsync(source_file.fileno())
                partial.hardlink_to(candidate)
                directory_fd = os.open(partial.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                raise OSError(f"Capture interrupted; recoverable partial retained at {partial.name}") from read_error
            raise read_error
        if not count or count % FRAME_BYTES:
            raise ValueError("Recording is empty or ends in a partial stereo frame")
        if before_publish is not None:
            before_publish()
        destination.hardlink_to(candidate)
    return count // FRAME_BYTES


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--maximum-seconds", type=int, required=True)
    parser.add_argument("--record", action="store_true", required=True)
    args = parser.parse_args()
    print(record_side(sys.stdin.buffer, args.destination, args.maximum_seconds, enabled=args.record))
