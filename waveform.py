"""Build compact stereo waveform peaks from a local PCM WAV."""
import argparse
import json
import struct
import wave


def waveform_peaks(reader, columns):
    if not 1 <= columns <= 10000:
        raise ValueError("Choose between 1 and 10,000 columns")
    if reader.getnchannels() != 2 or reader.getsampwidth() != 2:
        raise ValueError("Expected 16-bit stereo WAV")
    total = reader.getnframes()
    columns = min(total, columns)
    peaks = []
    for index in range(columns):
        start = index * total // columns
        end = (index + 1) * total // columns
        left = right = 0
        remaining = end - start
        while remaining:
            count = min(remaining, 4096)
            block = reader.readframes(count)
            if len(block) != count * 4:
                raise ValueError("Truncated WAV")
            for l_sample, r_sample in struct.iter_unpack("<hh", block):
                left = max(left, abs(l_sample))
                right = max(right, abs(r_sample))
            remaining -= count
        peaks.append([left, right])
    return {"sample_rate": reader.getframerate(), "frames": total, "peaks": peaks}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording")
    parser.add_argument("--columns", type=int, default=1000)
    args = parser.parse_args()
    with wave.open(args.recording, "rb") as recording:
        print(json.dumps(waveform_peaks(recording, args.columns)))
