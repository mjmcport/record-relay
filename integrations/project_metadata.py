"""Read status over an existing private SSH route and project track fields locally."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
from time import time, sleep


def project(destination, state):
    track = state.get("track")
    if not state.get("identify") or not state.get("signal") or not isinstance(track, dict):
        track = None
    else:
        track = {key: track[key][:300] for key in ("title", "artist", "album")
                 if isinstance(track.get(key), str)}
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
        path = Path(temporary.name)
        try:
            temporary.write(json.dumps({"updated_at": time(), "track": track}).encode())
            temporary.close()
            os.replace(path, destination)
        finally:
            path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("status_command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.status_command or not args.destination.parent.is_dir():
        parser.error("Provide a destination directory and a command that returns control status JSON.")
    while True:
        try:
            output = subprocess.check_output(args.status_command, timeout=5)
            project(args.destination, json.loads(output))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        sleep(2)


if __name__ == "__main__":
    main()
