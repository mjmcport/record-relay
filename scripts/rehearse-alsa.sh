#!/bin/sh
# Host-side check for Linux with ALSA and Sendspin CLI. Uses generated PCM only.
set -eu
umask 077
project=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
work=$(mktemp -d /tmp/record-relay.XXXXXXXX)
trap 'rm -rf -- "$work"' EXIT
cd "$work"
python3 -c 'import sys; sys.stdout.buffer.write(bytes(range(256)) * 750)' > original.pcm
python3 "$project/capture.py" < original.pcm > relayed.pcm
cmp original.pcm relayed.pcm

cat > asound.conf <<ALSA
</usr/share/alsa/alsa.conf>
pcm.record_relay_test {
    type file
    slave.pcm "null"
    file "/dev/null"
    infile "$work/relayed.pcm"
    format "raw"
}
ALSA
export ALSA_CONFIG_PATH="$work/asound.conf"
arecord -q -D record_relay_test -t raw -f S16_LE -c2 -r48000 -d1 observed.pcm
cmp original.pcm observed.pcm
printf '%s\n' 'PASS: virtual ALSA capture matches input bytes.'
