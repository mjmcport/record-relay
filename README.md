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

### Linux virtual-input compatibility

Run `sh scripts/rehearse-alsa.sh` on the Pi. This uses a process-local ALSA configuration and a finite generated file. No physical device, live source, or Music Assistant server is used. The input may be unlisted by the client's enumerator; explicit selection by name works in the installed [C++ Sendspin CLI](https://github.com/Sendspin/sendspin-cpp-cli). On a Pi 3 the client reported 48 kHz, 16-bit stereo for this virtual input in an isolated launch with no Music Assistant connection.

A finite file only tests format and byte parity. A live adapter must also pass sustained pacing, input reopen, EOF and reconnection tests. Do not deploy this file-backed device as a live source.

A paced FIFO substituted for the finite file failed on the Pi with a broken pipe during capture. Keep the production source intact. Next, evaluate a managed audio graph with a virtual microphone, or an upstream PCM source adapter, rather than treating the ALSA file plugin as a reliable live pipe.

### Recognition request policy

`recognition.LookupGate` is a provider-independent scheduler. It requires explicit enablement, audible signal and at least ten seconds of buffered audio. A qualifying gap makes a lookup eligible after cooldown, and a slower periodic check covers continuous material. Only one request can be in flight. Misses apply exponential backoff, and a longer provider retry delay takes precedence. The caller owns request timeouts and must report each outcome. The module makes no network requests or scrobble submissions.

### Managed virtual microphone

A user-local PulseAudio rehearsal on a Raspberry Pi 3 used a private Unix socket in a mode-0700 scratch directory, a 48 kHz stereo null sink, and two monitor clients. Six seconds of paced generated PCM reached the primary reader across a secondary reader disconnect and reconnect. The installed Sendspin CLI recognized `pulse:record_relay.monitor` as 48 kHz, 16-bit, stereo in a separate process with a temporary identity and no Music Assistant connection.

The next deployment rehearsal is generated audio through Sendspin into Music Assistant under a separate source identity. Persistently installing the audio service and switching the physical capture input are separate operations. Keep the current source available until the test source passes sustained playback and recovery checks, and run the physical-input cutover only at an agreed playback break.

`listening.ListeningInstance` provides a separate eligibility check for confirmed audible listening. Detection and listening are independent. The prototype uses half the known track length or four minutes, whichever is shorter, and excludes known tracks of 30 seconds or less. Unknown duration requires four verified minutes. Persisting receipts and sending to a listening service remain integration work.

The SQLite receipt store reserves a play before submission. A pending receipt blocks automatic resubmission even after a crash or restart. Mark it accepted only after provider confirmation. A crash before the request also leaves a pending receipt for manual review. Place the database in a private runtime directory outside the repository. The provider integration and receipt-review interface remain open.

### Export a reviewed split

With FFmpeg installed, `python3 export_track.py side-a.wav track-01.flac --start 48000 --end 144000 --title "Example"` exports an exact stereo sample range from a WAV side master. Start is inclusive and end is exclusive. The original is preserved, and an existing output is never replaced. This is a local CLI step; artwork, waveform editing and transfer verification are later features.
