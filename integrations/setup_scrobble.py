"""Create private Record Relay scrobble config using a hidden ListenBrainz prompt."""
import argparse
from getpass import getpass
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from music_assistant import MusicAssistantObserver
from session_cli import private_text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("ma_token_file", type=Path, nargs="?")
    parser.add_argument("music_assistant_url", nargs="?")
    parser.add_argument("player_id", nargs="?")
    parser.add_argument("source_uri", nargs="?")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("Run from an interactive Terminal for hidden credential entry.")
    details = args.output.parent.stat()
    if details.st_uid != os.getuid() or details.st_mode & 0o077 or args.output.exists():
        parser.error("Choose a new output path in a directory owned by you with mode 0700.")
    ma_values = (args.ma_token_file, args.music_assistant_url, args.player_id, args.source_uri)
    if any(ma_values) and not all(ma_values):
        parser.error("Provide all four optional Music Assistant arguments or none.")
    config = {}
    if all(ma_values):
        ma_token = private_text(args.ma_token_file).strip()
        try:
            MusicAssistantObserver(args.music_assistant_url, ma_token, args.player_id,
                                   args.source_uri, transport=None)
        except ValueError as error:
            parser.error(str(error))
        config.update({"music_assistant_url": args.music_assistant_url,
                       "music_assistant_token": ma_token, "player_id": args.player_id,
                       "source_uri": args.source_uri})
    token = getpass("ListenBrainz user token (hidden): ").strip()
    if not token or any(c.isspace() for c in token):
        parser.error("The ListenBrainz token must be nonempty and contain no whitespace.")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with os.fdopen(os.open(args.output, flags, 0o600), "w") as config_file:
        config["listenbrainz_token"] = token
        json.dump(config, config_file)
    print("Private configuration saved. Scrobble remains off until enabled in the panel.")


if __name__ == "__main__":
    main()
