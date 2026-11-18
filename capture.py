"""One PCM producer, one playback sink and an independent recent-audio buffer."""
import argparse
from collections import deque
import io
import json
from pathlib import Path
import sys
from threading import Lock
import wave

RATE = 48000
FRAME_BYTES = 4
BLOCK_BYTES = RATE * FRAME_BYTES // 10


class RecentAudio:
    def __init__(self, seconds=15):
        if not 1 <= seconds <= 60:
            raise ValueError("Buffer duration must be between 1 and 60 seconds")
        self.limit = seconds * RATE * FRAME_BYTES
        self.blocks = deque()
        self.size = 0
        self.lock = Lock()

    def append(self, block):
        if len(block) % FRAME_BYTES:
            raise ValueError("Incomplete stereo frame")
        with self.lock:
            block = block[-self.limit:]
            self.blocks.append(block)
            self.size += len(block)
            while self.size > self.limit:
                excess = self.size - self.limit
                oldest = self.blocks.popleft()
                if len(oldest) > excess:
                    self.blocks.appendleft(oldest[excess:])
                    self.size -= excess
                else:
                    self.size -= len(oldest)

    def snapshot(self):
        # Copy references under the lock. Assemble the immutable audio outside it.
        with self.lock:
            blocks = tuple(self.blocks)
        return b"".join(blocks)

    def wav_snapshot(self):
        result = io.BytesIO()
        with wave.open(result, "wb") as writer:
            writer.setparams((2, 2, RATE, 0, "NONE", "not compressed"))
            writer.writeframes(self.snapshot())
        return result.getvalue()


def relay(source, playback, recent, *, recorder=None):
    """Pass complete frames through unchanged. Only the playback sink paces input."""
    pending = b""
    frames = 0
    while block := source.read(BLOCK_BYTES):
        pending += block
        usable = len(pending) - len(pending) % FRAME_BYTES
        if not usable:
            continue
        audio, pending = pending[:usable], pending[usable:]
        remaining = memoryview(audio)
        while remaining:
            sent = playback.write(remaining)
            if not sent:
                raise OSError("Playback sink did not accept audio")
            remaining = remaining[sent:]
        playback.flush()
        recent.append(audio)
        if recorder is not None:
            recorder.offer(audio)
        frames += usable // FRAME_BYTES
    if pending:
        raise ValueError("Input ended with an incomplete stereo frame")
    return frames


def main():
    parser = argparse.ArgumentParser(description="Relay 48 kHz signed 16-bit stereo PCM.")
    parser.add_argument("--snapshot", type=Path,
                        help="Explicitly save the last 15 seconds as WAV after EOF; must not exist")
    parser.add_argument("--record-side", type=Path,
                        help="Opt in to writing a WAV side master")
    parser.add_argument("--record-limit", type=int, default=1800,
                        help="Maximum seconds to record")
    args = parser.parse_args()
    if args.record_side and not 1 <= args.record_limit <= 3600:
        parser.error("--record-limit must be 1–3600 seconds")
    recorder = None
    if args.record_side:
        from fanout import BackgroundRecording
        recorder = BackgroundRecording(args.record_side, args.record_limit)
    recent = RecentAudio()
    try:
        frames = relay(sys.stdin.buffer, sys.stdout.buffer, recent, recorder=recorder)
    finally:
        if recorder:
            try:
                recorded = recorder.stop()
                print(f"Recorded {recorded} frames.", file=sys.stderr)
            except (OSError, ValueError) as error:
                print(f"Recording failed: {error}", file=sys.stderr)
    if args.snapshot:
        with args.snapshot.open("xb") as output:
            output.write(recent.wav_snapshot())
    print(json.dumps({"frames_relayed": frames, "buffer_bytes": recent.size}), file=sys.stderr)


if __name__ == "__main__":
    main()
