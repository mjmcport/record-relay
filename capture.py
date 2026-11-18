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
        if not 1 <= seconds <= 600:
            raise ValueError("Buffer duration must be between 1 and 600 seconds")
        self.limit = seconds * RATE * FRAME_BYTES
        self.blocks = deque()
        self.size = 0
        # Absolute stereo-frame coordinates let a late save name the exact
        # retained interval. The generation changes only if the caller resets
        # the ring; ordinary rollover never changes it.
        self.generation = 0
        self.oldest_frame = 0
        self.newest_frame = 0
        self.lock = Lock()

    def append(self, block):
        if len(block) % FRAME_BYTES:
            raise ValueError("Incomplete stereo frame")
        with self.lock:
            block = block[-self.limit:]
            self.blocks.append(block)
            self.size += len(block)
            self.newest_frame += len(block) // FRAME_BYTES
            while self.size > self.limit:
                excess = self.size - self.limit
                oldest = self.blocks.popleft()
                if len(oldest) > excess:
                    self.blocks.appendleft(oldest[excess:])
                    self.size -= excess
                else:
                    self.size -= len(oldest)
            self.oldest_frame = self.newest_frame - self.size // FRAME_BYTES

    def snapshot_blocks(self):
        with self.lock:
            return tuple(self.blocks)

    def snapshot_range(self, start_frame, end_frame=None, *, generation=None):
        """Return immutable PCM for an available half-open absolute frame range.

        Bounds are checked under the same lock as the block snapshot, so a
        caller gets either the exact requested window or a stale-range error;
        it can never receive a silently shortened prefix after ring rollover.
        """
        with self.lock:
            if generation is not None and generation != self.generation:
                raise ValueError("Buffered audio generation changed")
            end_frame = self.newest_frame if end_frame is None else end_frame
            if (type(start_frame) is not int or type(end_frame) is not int or
                    start_frame < self.oldest_frame or end_frame > self.newest_frame or
                    end_frame <= start_frame):
                raise ValueError("Requested buffered audio range is unavailable")
            offset = (start_frame - self.oldest_frame) * FRAME_BYTES
            length = (end_frame - start_frame) * FRAME_BYTES
            blocks = tuple(self.blocks)
        joined = b"".join(blocks)
        return joined[offset:offset + length]

    def snapshot_selection(self, start_frame, end_frame=None, *, generation=None):
        """Return selected prefix blocks and their validated absolute bounds.

        The returned blocks are immutable bytes suitable for BackgroundRecording
        prefix input; live frames offered after the caller attaches that writer
        begin at the returned ``end_frame`` edge.
        """
        with self.lock:
            if generation is not None and generation != self.generation:
                raise ValueError("Buffered audio generation changed")
            selected_end = self.newest_frame if end_frame is None else end_frame
            if (type(start_frame) is not int or type(selected_end) is not int or
                    start_frame < self.oldest_frame or selected_end > self.newest_frame or
                    selected_end <= start_frame):
                raise ValueError("Requested buffered audio range is unavailable")
            offset = (start_frame - self.oldest_frame) * FRAME_BYTES
            length = (selected_end - start_frame) * FRAME_BYTES
            blocks = tuple(self.blocks)
            selection = {"generation": self.generation, "start_frame": start_frame,
                         "end_frame": selected_end}
        audio = b"".join(blocks)[offset:offset + length]
        return selection, (audio,)

    def bounds(self):
        with self.lock:
            return {"generation": self.generation,
                    "oldest_frame": self.oldest_frame,
                    "newest_frame": self.newest_frame}

    def clear(self):
        with self.lock:
            self.blocks.clear()
            self.size = 0
            self.generation += 1
            self.oldest_frame = self.newest_frame

    def snapshot(self):
        # Copy references under the lock. Assemble the immutable audio outside it.
        with self.lock:
            blocks = tuple(self.blocks)
        return b"".join(blocks)

    def wav_snapshot(self, *, frames=None):
        result = io.BytesIO()
        with wave.open(result, "wb") as writer:
            writer.setparams((2, 2, RATE, 0, "NONE", "not compressed"))
            audio = self.snapshot()
            if frames is not None:
                audio = audio[-frames * FRAME_BYTES:]
            writer.writeframes(audio)
        return result.getvalue()


def relay(source, playback, recent, *, recorder=None, on_playback_error=None):
    """Relay complete frames, keeping capture alive if playback fails.

    The source remains the pacing source after a playback error. The optional
    sink is detached once and reported; recent audio and the recorder continue
    receiving every complete input frame. Return value remains the historical
    synchronous frame count.
    """
    pending = b""
    frames = 0
    playback_live = True
    while True:
        try:
            block = source.read(BLOCK_BYTES)
        except Exception as error:
            if recorder is not None and hasattr(recorder, "interrupt"):
                recorder.interrupt(error)
            raise
        if not block:
            break
        pending += block
        usable = len(pending) - len(pending) % FRAME_BYTES
        if not usable:
            continue
        audio, pending = pending[:usable], pending[usable:]
        if playback_live:
            try:
                remaining = memoryview(audio)
                while remaining:
                    sent = playback.write(remaining)
                    if not sent:
                        raise OSError("Playback sink did not accept audio")
                    remaining = remaining[sent:]
                playback.flush()
            except (OSError, BrokenPipeError, ValueError) as error:
                playback_live = False
                if on_playback_error is not None:
                    try:
                        on_playback_error(error)
                    except Exception:
                        # Diagnostics must not take down the capture path.
                        pass
                else:
                    print(f"Playback sink unavailable; capture continues: {error}",
                          file=sys.stderr)
        recent.append(audio)
        if recorder is not None:
            recorder.offer(audio)
        frames += usable // FRAME_BYTES
    if pending:
        error = ValueError("Input ended with an incomplete stereo frame")
        if recorder is not None and hasattr(recorder, "interrupt"):
            recorder.interrupt(error)
        raise error
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
