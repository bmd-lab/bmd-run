"""Fixture provenance: committed fixtures match MANIFEST.json, and the capture tool stays external.

Byte-for-byte regeneration against a live Compute v1.0.0 is a manual reviewer
step (``tools/fixtures_v1.py capture`` then ``compare``). These tests check
everything that does not need a running Compute.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import unittest

from _support import FIXTURES, ROOT, TOKEN_RE, UUID_RE

TOOL = ROOT / "tools" / "fixtures_v1.py"
spec = importlib.util.spec_from_file_location("fixtures_v1", TOOL)
fixtures_v1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures_v1)

TOOL_ALLOWED_IMPORTS = {
    "__future__", "argparse", "hashlib", "json", "re", "sys", "pathlib",
    "bmd_run.endpoints", "bmd_run.transport", "bmd_run.errors",
}


class Manifest(unittest.TestCase):
    def test_committed_fixtures_match_manifest(self):
        recorded = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))["files"]
        self.assertEqual(recorded, fixtures_v1.manifest_entries())

    def test_every_case_has_a_fixture_and_every_fixture_a_case(self):
        cases = {case["file"] for case in fixtures_v1.load_cases()}
        on_disk = {p.name for p in FIXTURES.iterdir()} - {
            "cases.json", "MANIFEST.json", "RECORDING.md", "Si.POSCAR", "NiO.POSCAR"}
        self.assertEqual(cases, on_disk)
        for case in fixtures_v1.load_cases():
            self.assertIn(case["endpoint"], {"OPENAPI", "ANALYZE", "BUILD_WORKFLOW"})

    def test_normalisation_removes_all_per_request_values(self):
        for case in fixtures_v1.load_cases():
            text = fixtures_v1.normalise((FIXTURES / case["file"]).read_text(encoding="utf-8"))
            for value in TOKEN_RE.findall(text):
                self.assertEqual(value, "<IDENTITY-TOKEN>", case["file"])
            self.assertIsNone(UUID_RE.search(text), case["file"])
            self.assertEqual(fixtures_v1.normalise(text), text)  # idempotent

    def test_only_build_pages_need_normalisation(self):
        for case in fixtures_v1.load_cases():
            text = (FIXTURES / case["file"]).read_text(encoding="utf-8")
            needs = fixtures_v1.normalise(text) != text
            self.assertEqual(needs, case["file"].startswith("build_") and case["status"] == 200, case["file"])


class ToolStaysExternal(unittest.TestCase):
    def test_tool_imports_no_compute_code_and_uses_only_the_client_transport(self):
        tree = ast.parse(TOOL.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module]
            else:
                continue
            for name in names:
                self.assertIn(name, TOOL_ALLOWED_IMPORTS, f"tools/fixtures_v1.py imports {name}")

    def test_tool_requests_go_through_the_closed_table(self):
        source = TOOL.read_text(encoding="utf-8")
        self.assertIn("transport.request(endpoint", source)
        for forbidden in ("http.client", "urllib.request", "socket", "subprocess", "/submit", "/prepare-remote",
                          "/monitor", "/resume", "backend.", "import main"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
