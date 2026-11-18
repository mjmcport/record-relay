"""Offline PCM pilot for the Record Relay turntable appliance."""
import argparse
import json
import math
from pathlib import Path
import struct
import wave

try:
    import numpy
except ImportError:
    numpy = None


def find_transitions(source, minimum_gap=0.8, threshold_db=-45, *, minimum_spacing=0):
    """Yield candidate track starts in seconds from a stereo PCM WAV."""
    if source.getnchannels() != 2 or source.getsampwidth() != 2:
        raise ValueError("Expected 16-bit stereo WAV")
    rate = source.getframerate()
    block_size = max(1, rate // 10)
    frame = 0
    quiet_frames = 0
    previous_start = None
    while block := source.readframes(block_size):
        frames = len(block) // 4
        if numpy is not None:
            samples = numpy.frombuffer(block, dtype="<i2").astype("float64")
            rms = math.sqrt(float(numpy.dot(samples, samples)) / (frames * 2))
        else:
            samples = struct.iter_unpack("<h", block)
            rms = math.sqrt(sum(value * value for (value,) in samples) / (frames * 2))
        if rms < 32768 * 10 ** (threshold_db / 20):
            quiet_frames += frames
        else:
            if previous_start is None or (quiet_frames >= minimum_gap * rate
                    and frame - previous_start >= minimum_spacing * rate):
                yield round(frame / rate, 3)
                previous_start = frame
            quiet_frames = 0
        frame += frames


def main():
    parser = argparse.ArgumentParser(description="Suggest track starts from a local side recording.")
    parser.add_argument("source", type=Path)
    parser.add_argument("--minimum-gap", type=float, default=0.8)
    parser.add_argument("--threshold-db", type=float, default=-45)
    args = parser.parse_args()
    if not math.isfinite(args.minimum_gap) or args.minimum_gap <= 0:
        parser.error("--minimum-gap must be positive and finite")
    if not -96 <= args.threshold_db <= 0:
        parser.error("--threshold-db must be between -96 and 0")
    with wave.open(str(args.source), "rb") as source:
        starts = list(find_transitions(source, args.minimum_gap, args.threshold_db))
    print(json.dumps({"candidate_starts_seconds": starts}, indent=2))


if __name__ == "__main__":
    main()
