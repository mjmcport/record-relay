# Record Relay

An offline prototype for reviewing likely track boundaries in a digitized record side. It reads a local 16-bit stereo PCM WAV and prints candidate starts. Playback, recognition services and the capture device are outside this prototype.

```sh
python3 relay.py side-a.wav --minimum-gap 0.8 --threshold-db -45
python3 -m unittest discover -s tests
```

Long quiet passages can produce false splits and gapless tracks may produce none. Review each boundary before exporting. Do not commit recordings, credentials or private installation details.

## Roadmap

1. Confirm the detector and CPU budget on a Pi with generated PCM. Begin live integration at an agreed playback break. Keep the existing source definition ready for rollback.
2. Give one process ownership of ALSA capture. Feed Sendspin from it and provide bounded queues for a rolling recognition buffer and an opt-in recorder. Drop or stop slow secondary consumers without blocking playback. Verify a real stereo signal and compare playback continuity with recognition and recording on and off.
3. Add a local control panel with distinct Identify, Scrobble and Record controls. Default all three off. Use a gap or an acoustic change to nominate a lookup, cap outbound requests, and back off on misses and rate limits. Confirm changes across observations. Start with bounded periodic checks for gapless material. Keep detected metadata in the companion UI until a supported Music Assistant update path is proven.
4. Feed the same recognition logic from a muted Sendspin receiver for radio, podcasts and mixes. Gate scrobbles on actual audible playback and elapsed listening. Persist submission receipts and quarantine uncertain submissions to avoid duplicates.
5. Record native stereo losslessly and preserve each side master. Add a waveform, editable sample offsets, track tags and optional release artwork. Export to staging with checksums and retain the master through offload verification. Exercise disk-full and network-loss behavior before enabling recording on the appliance.

## Research and provenance

The prototype is original. No external project code, assets or audio were incorporated. Treat these projects as design references and check the license at a pinned revision before adopting anything.

- [Turntable Sendspin Bridge](https://github.com/jordancollins1/turntable-sendspin): capture once, then route the same audio to playback and recognition. No reuse permission assumed.
- [VinylFlow](https://github.com/olimic1000/vinylflow) and [vinyl-recorder](https://github.com/arthurbenemann/vinyl-recorder): review splitting, album metadata, and artwork workflows.
- [flacsplit](https://github.com/sarahrobiin/flacsplit): retain an editable split plan and export from an unchanged side master.
- [hark](https://github.com/omsingh02/hark): study recognition cadence and backoff. GPL code stays outside this MIT project.
- [Spindle](https://github.com/LuKresXD/Spindle) and [waxid](https://github.com/leolobato/waxid): compare recognition and scrobbling workflows. Keep suggested identification separate from verified listening.

## Shared capture rehearsal

`capture.py` accepts signed little-endian PCM at 48 kHz, two channels and 16 bits per sample. It writes the same complete frames to stdout and keeps only the latest 15 seconds in memory. Diagnostics go to stderr. With Python embedding, an independent consumer can take a snapshot without pacing the relay. The CLI can save an explicitly requested WAV snapshot after input ends.

```sh
ffmpeg -v error -f lavfi -i 'sine=frequency=440:duration=3' -ar 48000 -ac 2 -f s16le generated.pcm
python3 capture.py --snapshot rehearsal.wav < generated.pcm > relayed.pcm
cmp generated.pcm relayed.pcm
python3 relay.py rehearsal.wav
```

This rehearsal passed the Python tests on the Mac and a Raspberry Pi 3. Tests cover fragmented input, partial sink writes, exact playback bytes, bounded snapshots, and rejection of a truncated frame.

### Live cutover and rollback

- Find a supported PCM handoff into the installed Sendspin client or use a compatible capture adapter. This prototype has no verified live Sendspin adapter. Test generated audio on a separate identity first. Check real-time pacing and reconnects as well as sample equality.
- Record the existing source's launch settings and verify that the original service can be restarted. At an agreed playback break, stop that source and open ALSA exactly once with the new capture process. Keep remote lookups and recording off for the first test.
- Start playback on the test identity, check both channels and sustained playback, then compare with local memory sampling active. Never test by opening ALSA a second time while the production source is playing.
- If the audio drops or the device fails to reopen, stop the new process and restart the saved original service. Verify playback from the original Turntable source before leaving the test. The rehearsal does not install or change either service.
