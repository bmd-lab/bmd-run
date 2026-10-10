"""The protected local attempt store."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from _support import ROOT  # noqa: F401

from bmd_run import attempt_store
from bmd_run.attempt_store import AttemptStore, new_record
from bmd_run.errors import LocalStateError

ATTEMPT = "6f1d2c3b-4a59-4e68-8b7a-9c0d1e2f3a4b"
REQUEST = {"structure": {"format": "poscar", "text": "Si\n"}, "workflow": {"desired_output": "energy_only"}}
SOURCE = {"file_name": "Si.POSCAR", "sha256": "0" * 64, "format": "poscar"}


def record(**changes):
    value = new_record(attempt_id=ATTEMPT, api_origin="http://127.0.0.1:18000", request=dict(REQUEST),
                       structure_source=dict(SOURCE), expected_plan_digest="sha256:" + "a" * 64)
    value.update(changes)
    return value


@unittest.skipUnless(os.name == "posix", "the attempt store requires POSIX permissions")
class AttemptStoreSafety(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "state"
        self.store = AttemptStore(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_create_is_exclusive_and_private(self):
        path = self.store.create(record())
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.root).st_mode & 0o777, 0o700)
        with self.assertRaises(LocalStateError):
            self.store.create(record())
        self.assertEqual(self.store.load(ATTEMPT)["attempt_id"], ATTEMPT)

    def test_update_is_atomic_and_keeps_one_file(self):
        self.store.create(record())
        updated = self.store.load(ATTEMPT)
        updated["local_state"] = "prepared"
        self.store.update(updated)
        self.assertEqual(self.store.load(ATTEMPT)["local_state"], "prepared")
        self.assertEqual(sorted(p.name for p in (self.root / "attempts").iterdir()), [f"{ATTEMPT}.json"])

    def test_links_and_shared_directories_are_refused(self):
        os.mkdir(self.root, 0o755)
        os.chmod(self.root, 0o755)
        with self.assertRaises(LocalStateError):
            self.store.ensure()
        os.chmod(self.root, 0o700)
        os.symlink(self._tmp.name, self.root / "attempts")
        with self.assertRaises(LocalStateError):
            self.store.ensure()

    def test_linked_record_is_refused(self):
        self.store.ensure()
        target = Path(self._tmp.name) / "elsewhere.json"
        target.write_text(json.dumps(record()), encoding="utf-8")
        os.chmod(target, 0o600)
        os.symlink(target, self.root / "attempts" / f"{ATTEMPT}.json")
        with self.assertRaises(LocalStateError):
            self.store.load(ATTEMPT)

    def test_closed_record_schema(self):
        for bad in (
            record(token="bmdc1.x"),
            record(remote_path="/bmd/runs/x"),
            record(local_state="running"),
            record(expected_plan_digest="md5:1"),
            record(request_sha256="0" * 64),
        ):
            with self.assertRaises(LocalStateError):
                attempt_store.validate_record(bad)
        changed = record()
        changed["request"]["resources"] = {"cpus": 96}
        with self.assertRaises(LocalStateError):
            attempt_store.validate_record(changed)

    def test_checksum_detects_inconsistency_but_not_deliberate_rehashing(self):
        # Accepted threat model: the SHA-256 is a consistency check against accidental
        # corruption and partial edits, not tamper protection against the account owner.
        self.store.create(record())
        path = self.root / "attempts" / f"{ATTEMPT}.json"
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["request"]["resources"] = {"cpus": 96}
        path.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(LocalStateError):
            self.store.load(ATTEMPT)
        stored["request_sha256"] = attempt_store.request_sha256(stored["request"])
        path.write_text(json.dumps(stored), encoding="utf-8")
        self.assertEqual(self.store.load(ATTEMPT)["request"]["resources"], {"cpus": 96})

    def test_unknown_attempt_is_none(self):
        self.assertIsNone(self.store.load("2c3d4e5f-6a7b-4c8d-ae9f-0a1b2c3d4e5f"))
        with self.assertRaises(LocalStateError):
            self.store.path_for("../escape")


if __name__ == "__main__":
    unittest.main()
