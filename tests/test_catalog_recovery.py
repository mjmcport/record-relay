import hashlib
import os
import tempfile
import threading
import unittest
import wave
from pathlib import Path

from catalog import Catalog, Conflict, InvalidMaster


def make_wav(path, payload=b"\x01\x02\x03\x04" * 8):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(48000)
        writer.writeframes(payload)


class CatalogRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = self.root / "library"
        self.catalog = Catalog(self.root / "state" / "catalog.sqlite", self.library)
        self.project = self.catalog.create_project("Night record")

    def tearDown(self):
        self.temp.cleanup()

    def begin(self, request_id="cmd-1", name="side-a.wav"):
        return self.catalog.begin_take(
            request_id=request_id, project_id=self.project["id"], side="A",
            capture_generation="capture-1", boot_generation="boot-1",
            staging_locator=f".{name}.staging", intended_locator=name,
            expected_revision=1)

    def test_publication_is_immutable_and_idempotent(self):
        intent = self.begin()
        staging = self.library / intent["staging_locator"]
        make_wav(staging)
        receipt = self.catalog.publish_take("cmd-1", expected_frames=8)
        self.assertEqual(receipt["frames"], 8)
        self.assertEqual(self.catalog.publish_take("cmd-1"), receipt)
        self.assertTrue((self.library / "side-a.wav").exists())
        with self.assertRaises(Conflict):
            self.begin("cmd-2", "side-a.wav")

    def test_request_replay_and_digest_conflict(self):
        first = self.begin()
        self.assertEqual(self.begin(), first)
        with self.assertRaises(Conflict):
            self.catalog.begin_take(request_id="cmd-1", project_id=self.project["id"], side="B",
                                    capture_generation="capture-1", boot_generation="boot-1",
                                    staging_locator=".side-b.stage", intended_locator="side-b.wav")

    def test_revision_cas_keeps_current_value(self):
        changed = self.catalog.edit_project(self.project["id"], 1, label="Manual title")
        self.assertEqual(changed["revision"], 2)
        with self.assertRaises(Conflict):
            self.catalog.edit_project(self.project["id"], 1, label="Stale title")
        self.assertEqual(self.catalog.get_project(self.project["id"])["label"], "Manual title")

    def test_reconciliation_adopts_verified_publication_after_db_gap(self):
        intent = self.begin()
        staging = self.library / intent["staging_locator"]
        final = self.library / intent["intended_locator"]
        make_wav(staging)
        os.link(staging, final)
        outcomes = self.catalog.reconcile()
        self.assertEqual(outcomes[0]["state"], "committed")
        self.assertEqual(self.catalog.get_intent("cmd-1")["state"], "committed")

    def test_invalid_or_missing_writer_is_never_claimed_saved(self):
        intent = self.begin()
        (self.library / intent["staging_locator"]).write_bytes(b"partial")
        result = self.catalog.reconcile()[0]
        self.assertEqual(result["state"], "quarantined")
        self.assertNotEqual(self.catalog.get_intent("cmd-1")["state"], "committed")

    def test_two_catalog_instances_serialize_and_only_one_wins(self):
        second = Catalog(self.root / "state" / "catalog.sqlite", self.library)
        barrier = threading.Barrier(2)
        outcomes = []
        def starter(cat, request_id, name):
            barrier.wait()
            try:
                cat.begin_take(request_id=request_id, project_id=self.project["id"], side="A",
                               capture_generation="g", boot_generation="b",
                               staging_locator=f".{name}.tmp", intended_locator=name)
                outcomes.append("ok")
            except Conflict:
                outcomes.append("conflict")
        threads = [threading.Thread(target=starter, args=(self.catalog, "one", "one.wav")),
                   threading.Thread(target=starter, args=(second, "two", "two.wav"))]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertCountEqual(outcomes, ["ok", "conflict"])

    def test_legacy_import_preserves_master_hash_and_unknown_side(self):
        legacy = self.library / "old.wav"
        make_wav(legacy)
        before = hashlib.sha256(legacy.read_bytes()).hexdigest()
        imported = self.catalog.import_legacy_wav(legacy, project_id=self.project["id"])
        self.assertIsNone(imported["side"])
        self.assertEqual(imported["master_sha256"], before)
        self.assertEqual(hashlib.sha256(legacy.read_bytes()).hexdigest(), before)
        self.assertEqual(self.catalog.import_legacy_wav(legacy, project_id=self.project["id"])["id"], imported["id"])

    def test_legacy_draft_requires_exact_verified_master_identity(self):
        legacy = self.library / "old.wav"
        make_wav(legacy)
        take = self.catalog.import_legacy_wav(legacy, project_id=self.project["id"])
        draft = self.root / "draft.json"
        draft.write_text('{"master_sha256":"wrong","points":[]}')
        self.assertEqual(self.catalog.import_legacy_draft(take["id"], draft)["state"], "unmatched")
        draft.write_text('{"master_sha256":"' + take["master_sha256"] + '","points":[]}')
        self.assertFalse(self.catalog.import_legacy_draft(take["id"], draft)["duplicate"])
        self.assertTrue(self.catalog.import_legacy_draft(take["id"], draft)["duplicate"])

    def test_artifact_digest_addressed_and_bounded(self):
        artifact = self.catalog.store_artifact(self.project["id"], "cover", b"cover")
        self.assertEqual((self.library / artifact["locator"]).read_bytes(), b"cover")
        with self.assertRaises(ValueError):
            self.catalog.store_artifact(self.project["id"], "cover", b"123", max_bytes=2)


if __name__ == "__main__":
    unittest.main()
