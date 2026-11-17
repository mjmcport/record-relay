"""Export a reviewed sample range to FLAC while preserving the original side."""
import argparse
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import wave


def export_track(source, output, start, end, tags):
    if output.suffix.lower() != ".flac":
        raise ValueError("Output path must end in .flac")
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    with wave.open(str(source), "rb") as reader:
        if reader.getnchannels() != 2 or reader.getsampwidth() != 2:
            raise ValueError("Expected 16-bit stereo WAV")
        if not 0 <= start < end <= reader.getnframes():
            raise ValueError("Choose a nonempty sample range within the recording")
        if end - start < 16:
            raise ValueError("Select at least 16 sample frames for FLAC export")
        with TemporaryDirectory(dir=output.parent, prefix=".record-relay-") as scratch:
            pcm = Path(scratch) / "track.wav"
            encoded = Path(scratch) / "track.flac"
            reader.setpos(start)
            with wave.open(str(pcm), "wb") as writer:
                writer.setparams(reader.getparams())
                remaining = end - start
                while remaining:
                    frames = reader.readframes(min(remaining, 48000))
                    if not frames or len(frames) % 4:
                        raise ValueError("Truncated recording")
                    writer.writeframesraw(frames)
                    remaining -= len(frames) // 4
            command = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(pcm), "-c:a", "flac"]
            for key, value in tags.items():
                if value:
                    command.extend(["-metadata", f"{key}={value}"])
            subprocess.run([*command, str(encoded)], check=True)
            # Publish atomically without replacing a previous export.
            output.hardlink_to(encoded)


def main():
    parser = argparse.ArgumentParser(description="Export a reviewed segment from WAV to FLAC.")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--start", type=int, required=True, help="Starting sample frame, inclusive")
    parser.add_argument("--end", type=int, required=True, help="Ending sample frame, exclusive")
    for field in ("title", "artist", "album", "track"):
        parser.add_argument(f"--{field}")
    args = parser.parse_args()
    tags = {field: getattr(args, field) for field in ("title", "artist", "album", "track")}
    export_track(args.source, args.output, args.start, args.end, tags)


if __name__ == "__main__":
    main()
