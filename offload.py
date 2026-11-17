"""Copy a finished FLAC to staging and verify it before publication."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory


def digest(path):
    with path.open("rb") as audio:
        return hashlib.file_digest(audio, "sha256").hexdigest()


def offload(source, destination):
    if source.suffix.lower() != ".flac" or destination.suffix.lower() != ".flac":
        raise ValueError("Choose a finished FLAC file and a .flac destination")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    expected = digest(source)
    with TemporaryDirectory(dir=destination.parent, prefix=".record-relay-") as staging:
        candidate = Path(staging) / "verified.flac"
        with source.open("rb") as original, candidate.open("xb") as copy:
            shutil.copyfileobj(original, copy)
            copy.flush()
            os.fsync(copy.fileno())
        if digest(candidate) != expected or digest(source) != expected:
            raise OSError("Audio changed or failed checksum verification")
        destination.hardlink_to(candidate)
    return expected


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(offload(args.source, args.destination))
