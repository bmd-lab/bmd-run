"""Compute's phonon Desired Output (bmd-compute 93cfe16): projection, display and batch provenance.

Uses the plan recorded from Compute (``tests/fixtures/compute_api_v1_phonons``) and the
fake BMD Compute on 127.0.0.1. Nothing contacts a live VM or POWER.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import unittest

from _api_support import PHONON_FIXTURES, fixture
from _support import ROOT, schema_errors
from test_batch import BatchBase

from bmd_run import machine, machine_output
from bmd_run.errors import UnexpectedResponse

CASE = fixture("plan_si_poscar_phonons")
BODY, SENT = CASE["response"]["body"], CASE["request"]["body"]
MACHINE_SCHEMA = json.loads((ROOT / "src/bmd_run/schemas/machine-output-v1.schema.json").read_text(encoding="utf-8"))
COMPUTE_PROSE = (BODY["scientific_inputs"]["stages"][1]["phonon_plan"]["note"], "atomate2.vasp", "phonon-grade",
                 "get_supercell_size (automatic)")


def project(body=BODY):
    return machine.project_plan(body, SENT)


class Projection(unittest.TestCase):
    def test_fixture_provenance(self):
        manifest = json.loads((PHONON_FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["compute_commit"], "93cfe16b212d356e271e62c4805433d3a9f19046")
        for name, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256((PHONON_FIXTURES / name).read_bytes()).hexdigest(), digest)
        self.assertNotIn("bmdc1.", (PHONON_FIXTURES / "plan_si_poscar_phonons.json").read_text(encoding="utf-8"))

    def test_recipe_and_phonon_plan_are_projected(self):
        plan = project()
        self.assertEqual(plan["workflow"]["desired_output"], "phonons")
        self.assertEqual(plan["plan_digest"], BODY["plan_digest"])
        self.assertEqual([s["stage_type"] for s in plan["workflow"]["stages"]], ["relax", "phonon_forces"])
        relax, forces = plan["scientific_inputs"]["stages"]
        self.assertNotIn("phonon_plan", relax)
        self.assertEqual(forces["phonon_plan"], {
            "basis": "submitted_structure", "supercell_matrix": [[-2, 2, 2], [2, -2, 2], [2, 2, -2]],
            "supercell_atom_count": 64, "displacement_count": 1,
            "workflow_id": "bmd_compute.phonons.atomate2_finite_displacement", "workflow_version": 1,
            "displacement_angstrom": 0.01, "symprec": 1e-05, "sym_reduce": True,
            "supercell_min_length_angstrom": 10.0, "kpath_scheme": "seekpath",
        })
        self.assertEqual(forces["poscar"]["num_sites"], 64)

    def test_json_and_text_output(self):
        plan = project()
        document = machine_output.envelope(command="plan", api_url=None, request=None, result=plan, error=None)
        self.assertEqual(schema_errors(document, MACHINE_SCHEMA), [])
        text = machine_output.render_plan(plan) + machine_output.render_run_plan(plan, "POSCAR")
        json_text = machine_output.to_json(document)
        self.assertIn("WORKFLOW (BMD Compute: phonons, 2 stages)", text)
        self.assertIn("Workflow:     Phonons", text)
        self.assertIn("Phonon force calculations", text)
        self.assertIn("1 displacement on 64-atom supercells", text)
        self.assertIn("supercell matrix -2 2 2 / 2 -2 2 / 2 2 -2", text)
        for prose in COMPUTE_PROSE:
            self.assertNotIn(prose, text + json_text)

    def test_malformed_or_unknown_values(self):
        body = copy.deepcopy(BODY)
        body["scientific_inputs"]["stages"][1]["phonon_plan"]["supercell_matrix"] = [[1, 0, 0]]
        with self.assertRaises(UnexpectedResponse):
            project(body)
        body = copy.deepcopy(BODY)
        body["scientific_inputs"]["stages"][1]["phonon_plan"]["settings"]["kpath_scheme"] = "Secret Words"
        self.assertIsNone(project(body)["scientific_inputs"]["stages"][1]["phonon_plan"]["kpath_scheme"])

    def test_non_phonon_plans_are_unchanged(self):
        for name in ("plan_si_poscar_energy_only", "plan_nio_poscar_electronic_dos", "plan_si_custom_relax_static"):
            case = fixture(name)
            plan = machine.project_plan(case["response"]["body"], case["request"]["body"])
            self.assertTrue(all("phonon_plan" not in s for s in plan["scientific_inputs"]["stages"]), name)
            self.assertNotIn("Phonons", machine_output.render_run_plan(plan, "X") + machine_output.render_plan(plan))


class RunAndBatch(BatchBase):
    def test_single_run(self):
        code, out, err = self.run_cli(self.si, "--desired-output", "phonons", "--json")
        self.assertEqual(code, 0, err)
        document = json.loads(out)
        self.assertEqual(document["result"]["plan"]["workflow"]["desired_output"], "phonons")
        self.assertEqual(document["result"]["plan"]["plan_digest"], BODY["plan_digest"])
        self.assertEqual(json.loads(self.compute.requests[0]["body"]), SENT)
        code, out, _ = self.run_cli(self.si, "--desired-output", "phonons")
        self.assertIn("Workflow:     Phonons", out)
        self.assertIn("Phonons:      1 displacement on 64-atom supercells", out)
        self.assertEqual(self.compute.sbatch_calls, 2)

    def test_batch_keeps_the_phonon_desired_output(self):
        shutil.copy2(self.ws.files / "Si.POSCAR", self.campaign_dir / "structures" / "Si.POSCAR")
        manifest = self.manifest([{"cell": "si-phonons", "structure": "structures/Si.POSCAR",
                                   "desired_output": "phonons"}])
        code, document, _ = self.batch(manifest)
        self.assertEqual(code, 0)
        row = document["cells"][0]
        self.assertEqual((row["workflow"]["desired_output"], row["plan_digest"]), ("phonons", BODY["plan_digest"]))
        self.assertEqual([s["stage_type"] for s in row["workflow"]["stages"]], ["relax", "phonon_forces"])
        code, document, _ = self.batch("status", manifest, "--local")
        self.assertEqual(document["cells"][0]["workflow"]["desired_output"], "phonons")


if __name__ == "__main__":
    unittest.main()
