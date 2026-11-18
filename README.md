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

### Music Assistant routing rehearsal

On a Raspberry Pi 3 with Sendspin CLI 0.4.0 and Music Assistant 2.10.12, two disposable, authenticated Sendspin identities were used to verify routing. The source used the client's built-in tone and the receiver wrote PCM without driving a speaker. Music Assistant sent about 1.7 MB to that receiver during a short test. Both identities were stopped, unpaired and removed. Existing players and their groups were left alone.

This verifies the transport and routing independently of the virtual microphone. It does not yet verify the composed path from the tested PulseAudio monitor through Music Assistant, sustained network streaming, or a reconnect of the Sendspin transport. Those are the next acceptance checks before a physical-input cutover. Temporary tokens belong outside the repository and should be revoked after testing. In the published Music Assistant 2.10.12 source, revoking one named token disconnects only that token’s API sessions. Bulk user-token revocation is different and can remove that user’s bound Sendspin pairings. The test server advertises 2.10.12, but its deployed source hash was unavailable.

### Verify an offload before publishing it

`python3 offload.py track-01.flac /path/to/staging/track-01.flac` streams a finished export into staging, flushes it, and checks both copies against SHA-256 before making the destination visible. It preserves the source and refuses to replace an existing destination. The destination filesystem must support hard links. If it does not, use a local staging filesystem while the target-specific offload is evaluated. The command verifies bytes; the track exporter is responsible for producing valid FLAC.

### Combined routing acceptance

The private token used for the first routing test was discarded locally. Batch the remaining authenticated tests into one session with an operator-approved credential. Prefer an isolated Music Assistant instance with an independent account and disposable pairings, so temporary token revocation cannot affect household pairings. If using the live server, create ephemeral endpoints with fresh identity state, verify that the sink is independent, and revoke only the dedicated token after cleanup. Avoid bulk user-token revocation.

Feed generated stereo PCM into the private PulseAudio monitor, then play that source only on the disposable silent sink. Run for at least a minute. Check continuity across multiple tone changes, observe a secondary reader disconnect while the primary runs, then reconnect a disposable Sendspin endpoint and verify delivery resumes. Stop, unpair and remove the test endpoints. Leave the production Turntable source and Sonos group alone throughout.

### Generate a waveform for track review

Run `python3 waveform.py side-a.wav --columns 1000` to produce compact JSON with independent peaks for the two channels. The reader processes the side in small chunks. Each peak column spans a known range of sample frames, so a future editor can map a click to a split location. This step makes no changes to the master and needs no network access.

### Review splits in a browser

1. Generate waveform JSON with `python3 waveform.py side-a.wav > side-a.json`.
2. Run `python3 -m http.server 8765 --bind 127.0.0.1` from this repository and open `http://127.0.0.1:8765/review/`.
3. Select your WAV and its matching JSON. Listen or type a time in seconds, save each track start, and adjust or remove it as needed. Add a start at zero if you want the lead-in included.
4. Download the split plan. Its start and end values are sample frames ready for the FLAC export command. Files stay local to the browser. Matching duration is a sanity check, so select the JSON generated from the actual WAV.

Run `node --test tests/review.test.mjs` for the split-plan tests. The review tool is local-only and has no live capture or Music Assistant controls.

The review workflow passed an isolated Chrome test at 390 and 1280 pixels wide using a generated WAV. The test exercised file selection, adding and editing starts, downloading the exact split ranges, and removing a start without horizontal overflow.

`submission.submit_once` now connects eligibility to persistent receipts and a caller-supplied sender. The sender must return explicit confirmation. Tested senders are simulated and no account is connected by this module.

### Optional provider and recording pilots

- `recording.py side-a.wav --record --maximum-seconds 1800` writes a lossless WAV master from explicitly supplied native PCM on stdin. The session stops at EOF or the requested cap. Empty and partial-frame sessions fail before publication. It does not open an audio device. The output filesystem must support hard links.
- `providers.recognize_audd` is an optional adapter for [AudD](https://docs.audd.io/). It sends a bounded WAV excerpt only when enabled. AudD needs its own credential and consent to send a sample. The project does not yet provide a Shazam adapter or a live scheduler wiring the request gate to a provider.
- `providers.send_listen` implements [ListenBrainz single-listen submission](https://listenbrainz.readthedocs.io/en/latest/users/json.html). Use it behind `submission.submit_once` with a private receipt database. The adapter has been exercised with simulated transport only.
- `providers.cover_candidates` accepts a chosen MusicBrainz release ID and lists approved front artwork from the [Cover Art Archive](https://musicbrainz.org/doc/Cover_Art_Archive/API). Release search, previews, and embedding are separate steps still to be integrated.

The recording CLI is an independent consumer primitive. Before connecting it to live playback, put it behind a bounded queue and test that disk or recorder failures stop only the recording. Leave capture and playback on the original service until the combined generated-audio route passes.

### Record alongside a PCM relay

Add `--record-side side-a.wav --record-limit 1800` to `capture.py` to explicitly start a side recording from the relayed audio. The writer has a queue of 30 blocks (up to about three seconds at the default read size). If it falls behind, the recording fails and no partial master is published. The playback process can continue to write its PCM output. Recorder errors appear on stderr. Start and stop within a persistent capture session still need a control interface.

Add `--cover selected.jpg` (or a local PNG) to `export_track.py` to embed reviewed front artwork. Cover Art Archive lookup only lists candidates. The user chooses the correct release and supplies the local image. The exporter does not fetch images or change the side master.

### Recognition provider choices

The audio buffer and lookup policy are provider-independent. The earlier Shazam prototype remains a reference. Apple's supported [ShazamKit](https://developer.apple.com/shazamkit/) lists macOS and Android among its platforms but does not list Raspberry Pi Linux. An official Shazam route could use a separately authorized Mac companion, subject to Apple's entitlement requirements and end-to-end testing. This repository does not depend on an unofficial Shazam endpoint.

[AudD](https://audd.io/pricing/) is the headless-compatible HTTP alternative. It needs an API token and sends a short audio excerpt when enabled. Its published entry tier is \$5/month for 1,000 requests, and a 300-request trial is listed where available. No account or subscription has been created. Set a provider-side hard cap if piloting it. The repository currently tests simulated calls only.

### Control an active PCM pilot

Create a private runtime directory (mode 0700) and a recording folder, then run `python3 session_cli.py serve /path/to/private/control.sock /path/to/recordings` with PCM on stdin and a playback sink on stdout. In a second terminal, run `python3 session_cli.py control /path/to/private/control.sock '<action JSON>'`:

| Action JSON | Result |
| --- | --- |
| `{"action":"status"}` | Reports input signal, recognition state and recording state |
| `{"action":"record","name":"side-a.wav","limit":1800}` | Starts a new side master from subsequent samples |
| `{"action":"stop"}` | Finalizes the side, reporting captured frames |
| `{"action":"identify","enabled":true}` | Arms the recognition gate in this session |

The socket has no network listener. A CLI session without a configured provider stays offline and rejects Identify. An embedding process can schedule `Session.lookup_if_due` outside the playback thread and supply a bounded recognizer. A late match is discarded if Identify was turned off during the request. Stop the recording and inspect its result before offloading it. Integrated synthetic tests cover continuity, recording controls and recognition state. The physical input service is still controlled by the original Sendspin process.

### Open the pilot control panel

With the private PCM session running, use `python3 panel.py /path/to/private/control.sock` and open `http://127.0.0.1:8766/`. Run the panel on the same machine as the socket. For a Pi, forward that loopback port over SSH. The panel assumes a trusted local OS account and uses loopback, exact origin checks and a per-process control token. Keep it off publicly accessible interfaces.

Recording controls work without a provider. For authorized AudD testing, supply `--audd-token-file` to the session using an owner-only file outside the repository. Identification starts disabled. Enable it in the panel to run lookups in a worker outside the PCM path. Turning it off stops future lookups, although a request already sent cannot be withdrawn. The panel reports the last recognized artist and title.

`worker.ScrobbleDelivery` combines verified playback observations, the eligibility policy, and the ListenBrainz sender. Its caller must establish track identity and actual audible playback. The panel does not enable scrobbling. Tests use simulated playback observations and a fake sender. The live Music Assistant playback observer and standalone silent-listener controls remain to be wired.

For the browser smoke test, install Playwright for Python and Chromium, then run `python3 tests/browser/test_panel.py`. Set `CHROME_PATH` to use an existing Chrome binary. The test starts disposable local processes, passes generated PCM and verifies the output WAV and rejected control requests.
