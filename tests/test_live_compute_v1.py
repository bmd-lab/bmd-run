"""Optional live test against a running BMD Compute v1.0.0 (read/build only).

Skipped unless BMD_RUN_LIVE_URL is set, for example to a clean local
v1.0.0 checkout started with ``uvicorn main:app --port 8765``. These requests
are Analyze/Build only and have no remote side effects.
"""

from __future__ import annotations

import io
import json
import os
import unittest

from _support import FIXTURES, load_schema, schema_errors

from bmd_run import cli, errors

LIVE_URL = os.environ.get("BMD_RUN_LIVE_URL")
SCHEMA = load_schema()


def run(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    code = cli.main(["--json", "--compute-url", LIVE_URL] + argv, stdout=stdout, stderr=stderr)
    document = json.loads(stdout.getvalue())
    problems = schema_errors(document, SCHEMA)
    assert not problems, problems
    return code, document


@unittest.skipUnless(LIVE_URL, "set BMD_RUN_LIVE_URL to run against a live Compute v1.0.0")
class LiveComputeV1(unittest.TestCase):
    def test_identity_matches_v1_reference(self):
        code, document = run(["identity", "--strict"])
        self.assertEqual(code, errors.EXIT_OK, document["result"])
        self.assertFalse(document["result"]["verified_source_identity"])

    def test_options(self):
        code, document = run(["options"])
        self.assertEqual(code, 0)

    def test_analyze_and_plan(self):
        si = str(FIXTURES / "Si.POSCAR")
        code, document = run(["analyze", si])
        self.assertEqual(code, 0)
        code, document = run(["plan", "dos", si])
        self.assertEqual(code, 0)
        stages = document["result"]["resolved_workflow"]["stages"]
        self.assertEqual([(s["theory"], s["stage_type"]) for s in stages],
                         [("pbe", "relax"), ("hse06", "static"), ("hse06", "dos")])
        code, document = run(["plan", "energy", si, "--cpus", "7"])
        self.assertEqual(code, errors.EXIT_COMPUTE_REJECTED)
        code, document = run(["plan", "dos", str(FIXTURES / "NiO.POSCAR")])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
