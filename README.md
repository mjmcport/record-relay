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
