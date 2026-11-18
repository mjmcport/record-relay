#!/usr/bin/env bash
# Run as the same non-root user as the Sendspin client.
set -euo pipefail
umask 077
if [[ $# -lt 2 ]]; then
    echo "Usage: $0 PRIVATE_RUNTIME RECORDINGS [session options] < pcm.raw" >&2
    exit 2
fi
runtime=$1
library=$2
shift 2
project=$(cd "$(dirname "$0")/.." && pwd)
if [[ ! -d "$runtime" || -L "$runtime" || $(stat -c '%u:%a' "$runtime") != "$(id -u):700" ]]; then
    echo "Provide a private mode-0700 runtime directory owned by this account." >&2
    exit 2
fi
runtime=$(realpath "$runtime")
if [[ ! "$runtime" =~ ^/[a-zA-Z0-9_./-]+$ ]]; then
    echo "Choose a runtime directory without spaces or special characters." >&2
    exit 2
fi
scratch=$(mktemp -d "$runtime/graph.XXXXXXXX")
daemon=
cleanup() {
    if [[ -n "$daemon" ]]; then
        kill "$daemon" 2>/dev/null || true
        wait "$daemon" 2>/dev/null || true
    fi
    rm -rf -- "$scratch"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export PULSE_SERVER="unix:$runtime/pulse.sock"
export PULSE_RUNTIME_PATH="$scratch/run"
export PULSE_STATE_PATH="$scratch/state"
mkdir "$PULSE_RUNTIME_PATH" "$PULSE_STATE_PATH"
if [[ -e "$runtime/pulse.sock" || -L "$runtime/pulse.sock" ]]; then
    echo "A graph socket already exists. Check the original process before retrying." >&2
    exit 1
fi
printf 'load-module module-native-protocol-unix socket=%s/pulse.sock auth-anonymous=true\nload-module module-null-sink sink_name=record_relay rate=48000 channels=2\n' "$runtime" > "$scratch/graph.pa"
pulseaudio -n -F "$scratch/graph.pa" --daemonize=no --exit-idle-time=-1 --log-target=stderr &
daemon=$!
for _ in {1..50}; do
    [[ ! -S "$runtime/pulse.sock" ]] || break
    kill -0 "$daemon"
    sleep .1
done
pactl list short sources | grep -F record_relay.monitor >&2
python3 "$project/session_cli.py" serve "$runtime/control.sock" "$library" "$@" |
    pacat --playback --device=record_relay --raw --format=s16le --rate=48000 --channels=2 --latency-msec=50
