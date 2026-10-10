"""Provenance of the recorded machine-API fixtures (offline checks)."""

from __future__ import annotations

import hashlib
import json
import re
import unittest

from _api_support import API_FIXTURES


class MachineFixtureProvenance(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((API_FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))

    def test_manifest_matches_every_file(self):
        self.assertEqual(self.manifest["schema"], "bmd_run.compute_api_fixtures")
        self.assertEqual(self.manifest["compute_commit"], "e3fbb3beaf5c0084023df9fcdd76deea4edc4778")
        on_disk = {p.name for p in API_FIXTURES.iterdir() if p.is_file()} - {"MANIFEST.json", "RECORDING.md"}
        self.assertEqual(set(self.manifest["files"]), on_disk)
        for name, digest in self.manifest["files"].items():
            data = (API_FIXTURES / name).read_text(encoding="utf-8").encode("utf-8")
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest, name)

    def test_no_token_material_or_remote_details_were_recorded(self):
        token = re.compile(r"bmdc1\.[a-z0-9]{8,32}\.[A-Za-z0-9_-]{43}")
        for path in API_FIXTURES.glob("*.json"):
            text = path.read_text(encoding="utf-8")
            for match in token.finditer(text):
                self.assertTrue(match.group(0).endswith("A" * 43), f"{path.name}: real token material")
            for needle in ("/bmd/", "ssh", "sbatch ", "#SBATCH", "Authorization"):
                self.assertNotIn(needle, text, path.name)

    def test_attempt_bodies_are_what_bmd_run_sends(self):
        for path in API_FIXTURES.glob("attempt_*.json"):
            case = json.loads(path.read_text(encoding="utf-8"))
            body = case["request"]["body"]
            if body is not None:
                self.assertLessEqual(set(body), {"structure", "workflow", "resources", "expected_plan_digest", "submit"}, path.name)


if __name__ == "__main__":
    unittest.main()
