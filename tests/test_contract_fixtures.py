"""Guard the P0 versioned interface and its synthetic integration fixtures."""
import json
import struct
import unittest
from pathlib import Path


FIXTURES = Path(__file__).parent / "fixtures" / "p0"
LEGACY_STATUS_KEYS = {
    "suggested_filename", "buffered_seconds", "buffer_limit_seconds", "identify",
    "scrobble", "signal", "recording", "track", "local_listening", "track_epoch",
    "recognition_timing",
}


def load_json(path):
    return json.loads(path.read_text())


class ContractFixtureTests(unittest.TestCase):
    def test_fixture_manifest_resolves_all_scenarios_and_assets(self):
        manifest = load_json(FIXTURES / "manifest.json")
        self.assertEqual(manifest["fixture_version"], 1)
        for path in manifest["scenarios"].values():
            self.assertTrue((FIXTURES / path).is_file(), path)
        for name in ("pcm", "cover", "legacy_draft"):
            self.assertTrue((FIXTURES / manifest["assets"][name]).is_file(), name)

    def test_compilation_fixture_keeps_physical_side_and_track_identity_separate(self):
        album = load_json(FIXTURES / "album_compilation_ab.json")
        self.assertEqual([take["side"] for take in album["takes"]], ["A", "B"])
        self.assertEqual(album["takes"][0]["tracks"][0]["performer"], "The Example Band")
        self.assertNotEqual(album["album_artist"], album["takes"][0]["tracks"][0]["performer"])
        self.assertEqual(album["takes"][0]["project_id"], album["project_id"])
        self.assertEqual(album["takes"][1]["project_id"], album["project_id"])

    def test_offline_and_missing_service_are_independent_of_capture_state(self):
        offline = load_json(FIXTURES / "standalone_no_services.json")
        missing = load_json(FIXTURES / "missing_optional_ma.json")
        self.assertFalse(offline["music_assistant"]["configured"])
        self.assertFalse(offline["recognition"]["configured"])
        self.assertTrue(offline["input"]["connected"])
        self.assertFalse(offline["input"]["signal"])
        self.assertEqual(missing["capture"]["state"], "recording")
        self.assertFalse(missing["music_assistant"]["available"])

    def test_two_tab_edits_use_distinct_request_ids_and_same_revision(self):
        conflict = load_json(FIXTURES / "two_tabs_conflict.json")
        first, second = conflict["commands"]
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertEqual(first["expected"]["revision"], conflict["initial_revision"])
        self.assertEqual(second["expected"]["revision"], conflict["initial_revision"])
        self.assertEqual(first["action"], second["action"])

    def test_versioned_snapshot_has_frame_truth_and_stable_generation(self):
        snapshot = load_json(FIXTURES / "capture_snapshot_v1.json")
        required = {
            "schema_version", "snapshot_id", "observed_at", "snapshot_age_ms", "capabilities",
            "project_id", "take_id", "capture_generation", "boot_generation", "side", "state",
            "sample_rate", "included_prefix_frames", "accepted_frames", "published_frames",
            "limit_frames", "ring", "latest_operation", "latest_error",
        }
        self.assertTrue(required.issubset(snapshot))
        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(snapshot["state"], "recording")
        self.assertEqual(snapshot["side"], "B")
        self.assertIsNone(snapshot["published_frames"])
        self.assertLessEqual(snapshot["ring"]["oldest_frame"], snapshot["ring"]["newest_frame"])

    def test_idempotency_fixture_replays_receipt_and_conflicts_on_payload_change(self):
        cases = load_json(FIXTURES / "idempotency_v1.json")
        replay = cases["replay_same_payload"]
        self.assertEqual(replay["command"]["request_id"], replay["expected_receipt"]["request_id"])
        self.assertEqual(replay["expected_receipt"]["state"], "succeeded")
        changed = cases["reused_id_different_payload"]
        self.assertEqual(changed["request_id"], replay["command"]["request_id"])
        self.assertEqual(changed["expected_state"], "conflict")

    def test_legacy_draft_and_status_projection_are_preserved(self):
        draft = load_json(FIXTURES / "legacy_draft.json")
        self.assertEqual(set(draft), {
            "frames", "sample_rate", "points", "tags", "confirmed", "artist",
            "album", "format", "cover", "selected",
        })
        status = load_json(FIXTURES / "legacy_snapshot.json")["status"]
        self.assertTrue(LEGACY_STATUS_KEYS.issubset(status))
        self.assertIn(status["recording"], {"idle", "recording", "finished", "failed"})
        legacy = load_json(FIXTURES / "legacy_binary.json")
        self.assertEqual(legacy["baseline_revision"], "d4cbaa77bcecba48b4327e88fccb6bb9c49e3eb4")
        self.assertEqual({command["action"] for command in legacy["commands"]}, {"status", "record", "stop"})
        self.assertTrue(LEGACY_STATUS_KEYS.issubset(legacy["status_projection"]))

    def test_pcm_and_cover_are_small_valid_synthetic_assets(self):
        pcm = (FIXTURES / "stereo-impulses-silence.s16le").read_bytes()
        self.assertEqual(len(pcm) % 4, 0)
        frames = [struct.unpack_from("<hh", pcm, offset) for offset in range(0, len(pcm), 4)]
        self.assertEqual(frames[:2], [(12000, -9000), (-8000, 6000)])
        self.assertEqual(frames[-1], (0, 0))
        cover = (FIXTURES / "synthetic-cover.png").read_bytes()
        self.assertTrue(cover.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertLess(len(cover), 1024)

    def test_toolchain_pins_have_no_external_unit_test_dependencies(self):
        toolchain = load_json(Path(__file__).parent / "toolchain.json")
        self.assertEqual(toolchain["python"], "3.14.8")
        self.assertEqual(toolchain["node"], "26.11.1")
        self.assertEqual(toolchain["python_test_dependencies"], [])
        self.assertEqual(toolchain["javascript_test_dependencies"], [])


if __name__ == "__main__":
    unittest.main()
