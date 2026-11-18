"""Create private Record Relay scrobble config using a hidden ListenBrainz prompt."""
import argparse
from getpass import getpass
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from session_cli import private_text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("ma_token_file", type=Path)
    parser.add_argument("music_assistant_url")
    parser.add_argument("player_id")
    parser.add_argument("source_uri")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("Run from an interactive Terminal for hidden credential entry.")
    details = args.output.parent.stat()
    if details.st_uid != os.getuid() or details.st_mode & 0o077 or args.output.exists():
        parser.error("Choose a new output path in a directory owned by you with mode 0700.")
    ma_token = private_text(args.ma_token_file).strip()
    token = getpass("ListenBrainz user token (hidden): ").strip()
    if any(not value or any(c.isspace() for c in value) for value in (ma_token, token)):
        parser.error("Both tokens must be nonempty and contain no whitespace.")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with os.fdopen(os.open(args.output, flags, 0o600), "w") as config:
        json.dump({"music_assistant_url": args.music_assistant_url,
                   "music_assistant_token": ma_token, "listenbrainz_token": token,
                   "player_id": args.player_id, "source_uri": args.source_uri}, config)
    print("Private configuration saved. Scrobble remains off until enabled in the panel.")


if __name__ == "__main__":
    main()
