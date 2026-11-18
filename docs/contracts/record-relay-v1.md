# Record Relay P0 contracts (v1)

These additive JSON contracts are the handoff point for P1–P5. They describe
local appliance control and synthetic test data; they do not implement a new
server, catalog, recorder, or UI. All identifiers are opaque strings generated
by the server unless a field explicitly says otherwise. Frame intervals are
half-open `[start, end)` and count stereo sample frames, not bytes.

## Compatibility boundary

| Existing surface at baseline `d4cbaa77` | Compatibility promise |
| --- | --- |
| `session_cli.py control SOCKET JSON` and `serve SOCKET LIBRARY` | Keep owner-only Unix socket and newline-delimited JSON; keep legacy `action` requests (`status`, `identify`, `scrobble`, `local_listening`, `record`, `save_buffer`, `stop`) working. New clients send `schema_version: 1`, `request_id`, `expected` and `payload`; adapters may accept legacy requests without an idempotency guarantee. |
| `Session.status()` flat projection | Preserve existing keys and types, including `recording`, `buffered_seconds`, `buffer_limit_seconds`, `identify`, `scrobble`, `signal`, `track`, `local_listening`, `track_epoch`, and `recognition_timing`. Additive fields are permitted; consumers must ignore unknown keys. Never reinterpret `recording` values `idle`, `recording`, `finished`, `failed`. |
| `panel.py` routes | Keep `/recordings`, `/recordings/audio`, `/recordings/waveform`, `/recordings/export`, `/recordings/export-side`, and `/recordings/draft`, including current Origin/token protections and Range streaming. New versioned routes are additive. |
| Configuration | Recognition remains optional (`--audd-token-file`, `--shazam`); MA/ListenBrainz remains optional (`--scrobble-config`). No MA config is required for standalone capture. Keep `listens.sqlite` receipt compatibility separate from the future catalog. |
| Legacy review draft | Read `{frames,sample_rate,points,tags,confirmed,artist,album,format,cover,selected}` by master name. Import lazily and idempotently; never bind it to another same-named file without matching master identity. Preserve invalid/unmatched originals for recovery. |

## Versioned message shapes

The canonical JSON instances are in `tests/fixtures/p0/`. These field names and
meanings are frozen for v1; objects may gain optional fields, but required
fields and meanings cannot change without a new major schema version.

### Snapshot

Required keys: `schema_version` (integer `1`), `snapshot_id`, `observed_at`
(UTC RFC 3339), `snapshot_age_ms` (nonnegative integer), `capabilities` (named
booleans), `project_id`, `take_id`, `capture_generation`, `boot_generation`,
`side` (`A` or `B`), `state`, `sample_rate`, `included_prefix_frames`,
`accepted_frames`, `published_frames`, `limit_frames`, `ring`,
`latest_operation`, and `latest_error`. IDs may be null before allocation.

`state` is one of `waiting_input`, `retaining`, `recording`, `finalizing`,
`saved`, `failed`, `interrupted`, or `unavailable`. Silence with fresh input is
`waiting_input`; stale/missing telemetry is `unavailable`, never silent input.
`ring` is null or `{generation, oldest_frame, newest_frame}` with a half-open
absolute frame interval. `accepted_frames` includes accepted pre-roll plus live
frames. `published_frames` is null until closed, verified and published; it is
not inferred from the accepted count. A stale snapshot is labeled using
`snapshot_age_ms`; clients do not advance elapsed time from an old sample.
`latest_operation` is null or `{request_id, state, receipt_id}`. `latest_error`
is null or `{code, message, recoverable}` and must not contain secrets.

### Command and receipt

Command shape: `{schema_version:1, request_id, action, expected, payload}`.
`request_id` is a UUID. `expected` contains the caller's `project_id`,
`take_id`, `capture_generation`, and/or `revision` preconditions applicable to
the action. `payload` is action-specific and bounded. Reject unknown actions,
invalid sides, malformed IDs, oversized payloads and stale preconditions with a
typed error; do not guess the current take or side.

Receipt shape: `{schema_version:1, request_id, request_digest, state,
receipt_id, result, error}`. State is `accepted`, `pending`, `succeeded`,
`failed`, or `conflict`. Canonical request digest excludes transport details.
Replaying the same request id and digest returns the same receipt/result;
reusing an id with a different digest returns `conflict` and performs no
operation. A retried `stop` always resolves against its original take and can
never stop a later take. A lost response is queried/replayed with the same ID,
not replaced with a fresh command.

### History, CAS edits and job lease

Paginated history is a separate bounded read model, `{schema_version:1,
items:[...], next_cursor}`; it is not appended to the legacy status line.
Every editable catalog object has a monotonically increasing `revision`.
Edits carry `expected_revision`; a mismatch returns `conflict` and the current
object/revision without applying any part of the edit. A user explicitly
reapplies after reviewing that value.

Expensive work uses one shared lease across panel processes:
`{schema_version:1, job_id, owner_id, fence, input_identity, revision,
expires_at, state}`. Acquire succeeds only when no unexpired lease exists;
takeover after expiry increments `fence`. Publication must compare the current
fence and immutable input/revision, so an expired worker cannot publish late.
Job states: `queued`, `running`, `cancel_requested`, `succeeded`, `failed`,
`cancelled`.

### Recording publication intent

Before writer creation, persist `{schema_version:1, request_id, project_id,
take_id, capture_generation, side, staging_id, intended_locator,
expected_revision, state}`. States advance through `prepared`, `writing`,
`closed`, `verified`, `published`, `committed`; terminal alternatives are
`interrupted`, `failed`, `quarantined`. The output is exclusive and no-overwrite.
Publication is a same-filesystem atomic visibility step after WAV close, frame
validation, flush and sync; a directory sync precedes catalog commit. Startup
reconciliation matches only the stable staging/take identity. Verified output
with a missing final catalog receipt is adopted; partial or ambiguous output is
quarantined and never represented as a complete saved side. Details and
recovery actions are receipts, not inferred success from a timeout.

## Deterministic fixtures

`tests/fixtures/p0/manifest.json` maps each scenario to its JSON, generated
stereo PCM, old draft and baseline CLI/status projection, and synthetic 1×1 cover. The PCM is 48 kHz signed
16-bit little-endian stereo, with distinct left/right impulses and silence;
the cover is generated locally and has no third-party artwork. No real audio,
credentials, endpoints, device identifiers, or service responses are present.
The JSON fixture tests run with the Python standard library only. Do not use
fixtures as evidence of hardware routing, service availability or actual
listening.

## P0 → later owner boundaries

| Surface | P0 output | Next owner |
| --- | --- | --- |
| Message names, compatibility and fixtures | This document and fixture set; Python contract tests | P1 catalog / P2 capture / P2-S setup / P4 UI |
| `panel.py` dispatch, existing routes and `panel.html` | Inventory only; no runtime changes | Integration owner coordinates P1/P4 edits |
| `session.py`, `session_cli.py`, `capture.py`, `recording.py`, `fanout.py` | Legacy status/command inventory and snapshot/intent contract | P2 capture |
| `listening.py`, `worker.py`, `receipts.py`, `music_assistant.py` | Existing optional/config and receipt compatibility inventory | P3-S listening |
| `review/*`, `waveform.py`, `export_track.py` | Legacy draft shape and pinned test commands | P5 review/export |
| Schema/migrations and deployment examples | Contract only; no shared DB or deployment changes | P1 catalog / P8 integration |

P0 verification commands: `python3 -m unittest discover -s tests` and
`node --test tests/review.test.mjs`. The suite is stdlib-only for Python and
requires Node's built-in test runner for JS. Browser scripts require separate
Playwright/Chromium installation and are not part of these pinned commands.
Baseline execution passed all 51 Python tests when allowed to bind disposable
localhost sockets. A default-sandbox run passed 47/51; four existing tests
could not bind their local TCP servers because the runner denied localhost
socket creation (`test_appliance_link` once, `test_panel_recordings` twice,
`test_scrobble_http` once). After P0, the equivalent permission-approved run
passed all 60 Python tests; the nine new fixture/contract tests also pass in
the default sandbox. The four existing JS tests passed. This environment
restriction is unrelated to the fixture tests.
