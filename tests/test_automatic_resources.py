"""Compatibility with BMD Compute's Automatic Resource Selection R1 (plan digest version 2).

Uses plans and attempts recorded from Compute R1 (``tests/fixtures/compute_api_v1_automatic_resources``)
and the fake BMD Compute on 127.0.0.1. Nothing contacts a live VM or POWER. Runner selects no
resources: it omits what the user did not give and reports what Compute resolved.
"""

from __future__ import annotations

import copy
import hashlib
import json
import unittest

from _api_support import AUTOMATIC_RESOURCES_FIXTURES, fixture
from _support import ROOT, schema_errors
from test_batch import BatchBase

from bmd_run import machine, machine_output
from bmd_run.errors import UnexpectedResponse, UsageError

AUTO, PARTIAL, MANUAL = (fixture(n) for n in ("plan_si_energy_automatic", "plan_si_energy_partial",
                                              "plan_si_energy_manual_2400h"))
SUBMITTED = fixture("attempt_submit_automatic")
MACHINE_SCHEMA = json.loads((ROOT / "src/bmd_run/schemas/machine-output-v1.schema.json").read_text(encoding="utf-8"))
RUN_SCHEMA = json.loads((ROOT / "src/bmd_run/schemas/run-output-v1.schema.json").read_text(encoding="utf-8"))
EXPLANATION = AUTO["response"]["body"]["resource_selection"]["walltime"]["explanation"]


def project(case, body=None):
    return machine.project_plan(body if body is not None else case["response"]["body"], case["request"]["body"])


class Projection(unittest.TestCase):
    def test_fixture_provenance(self):
        manifest = json.loads((AUTOMATIC_RESOURCES_FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["compute_commit"], "90d1c2391760af03730769efd1e23285d46f1b30")
        for name, digest in manifest["files"].items():
            data = (AUTOMATIC_RESOURCES_FIXTURES / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest, name)
            self.assertNotIn(b"bmdc1.", data)

    def test_digest_v2_plan_with_qos_and_selection(self):
        plan = project(AUTO)
        body = AUTO["response"]["body"]
        self.assertEqual((plan["plan_digest"], plan["plan_digest_version"]), (body["plan_digest"], 2))
        self.assertEqual(plan["resources"], {"nodes": 1, "cpus": 24, "memory_gb": 128, "walltime": "01:00:00",
                                             "partition": "leeburton-pool", "account": "power-leeburton-users_v2",
                                             "qos": "owner100d"})
        self.assertEqual(plan["policies"]["automatic_resources"], 1)
        self.assertEqual(plan["resource_selection"], {
            "policy": {"id": "bmd_compute.automatic_resources", "version": 1},
            "sources": {"cpus": "automatic", "memory_gb": "automatic", "walltime": "automatic", "queue": "automatic"},
            "walltime": {"recommended": "01:00:00", "basis": "empirical_estimate",
                         "stages": [{"index": 1, "class": "calibrated", "seconds": 277.354}]},
        })
        self.assertEqual(project(PARTIAL)["resource_selection"]["sources"]["cpus"], "manual")
        document = machine_output.envelope(command="plan", api_url=None, request=None, result=plan, error=None)
        self.assertEqual(schema_errors(document, MACHINE_SCHEMA), [])
        text = machine_output.render_plan(plan) + machine_output.render_run_plan(plan, "POSCAR")
        self.assertIn("walltime 01:00:00, QOS owner100d", text)
        self.assertIn("QOS:        owner100d", text)
        self.assertIn("automatic: CPUs, memory, walltime; given: none (walltime basis: empirical estimate)", text)
        self.assertNotIn(EXPLANATION, text + machine_output.to_json(document))

    def test_four_digit_walltime(self):
        plan = project(MANUAL)
        self.assertEqual((plan["resources"]["walltime"], plan["resources"]["cpus"], plan["resources"]["memory_gb"]),
                         ("2400:00:00", 48, 64))
        self.assertIn("walltime 2400:00:00", machine_output.render_run_plan(plan, "POSCAR"))
        self.assertIn("given: CPUs, memory, walltime", machine_output.render_run_plan(plan, "POSCAR"))
        self.assertEqual(machine.resources_request(cpus=None, memory_gb=None, walltime="2400:00:00", queue=None),
                         {"walltime": "2400:00:00"})
        with self.assertRaises(UsageError):
            machine.resources_request(cpus=None, memory_gb=None, walltime="24000:00:00", queue=None)

    def test_walltime_boundaries(self):
        for value in ("1:00:00", "999:59:59", "1000:00:00", "2400:00:00"):
            self.assertEqual(machine.resources_request(cpus=None, memory_gb=None, walltime=value, queue=None),
                             {"walltime": value})
        for value in ("2400:00:01", "2401:00:00", "9999:59:59", "24000:00:00", "10:60:00"):
            with self.assertRaises(UsageError, msg=value):
                machine.resources_request(cpus=None, memory_gb=None, walltime=value, queue=None)
        for value, shown in (("999:59:59", "999:59:59"), ("1000:00:00", "1000:00:00"), ("2400:00:00", "2400:00:00"),
                             ("1:00:00", "01:00:00")):
            body = copy.deepcopy(MANUAL["response"]["body"])
            body["resources"]["walltime"] = value
            self.assertEqual(project(MANUAL, body)["resources"]["walltime"], shown)
        for value in ("2400:00:01", "24000:00:00"):
            body = copy.deepcopy(MANUAL["response"]["body"])
            body["resources"]["walltime"] = value
            with self.assertRaises(UnexpectedResponse, msg=value):
                project(MANUAL, body)

    def test_published_schemas_accept_the_2400_hour_plan(self):
        plan = project(MANUAL)
        for walltime in ("2400:00:00", "1000:00:00", "999:59:59", "1:00:00"):
            sent = copy.deepcopy(MANUAL["request"]["body"])
            sent["resources"]["walltime"] = walltime
            # As the CLI builds it: the request summary plus the structure source.
            request = {**machine.request_summary(sent),
                       "structure_source": {"file_name": "Si.POSCAR", "sha256": "0" * 64, "format": "poscar"}}
            machine_document = machine_output.envelope(command="plan", api_url=None, request=request, result=plan,
                                                       error=None)
            self.assertEqual(schema_errors(machine_document, MACHINE_SCHEMA), [], walltime)
            run_document = machine_output.run_envelope(
                api_url=None, request=request, progress={"stage": "plan", "plan": plan, "attempt_id": None},
                error=None)
            self.assertEqual(schema_errors(run_document, RUN_SCHEMA), [], walltime)
        self.assertEqual(request["resources"]["walltime"], "1:00:00")

    def test_digest_v1_plans_and_older_attempts_are_unchanged(self):
        case = fixture("plan_si_poscar_energy_only")
        plan = project(case)
        self.assertEqual(plan["plan_digest_version"], 1)
        self.assertNotIn("qos", plan["resources"])
        self.assertNotIn("resource_selection", plan)
        self.assertNotIn("automatic_resources", plan["policies"])
        text = machine_output.render_run_plan(plan, "X") + machine_output.render_plan(plan)
        self.assertNotIn("QOS", text)
        self.assertNotIn("Selection", text)
        old = fixture("attempt_get_submitted")
        attempt = machine.project_attempt(old["response"]["body"], attempt_id=old["request"]["path"].rsplit("/", 1)[1],
                                          expected_plan_digest=None)
        self.assertNotIn("qos", attempt["resources"])

    def test_r1_attempt_reports_qos(self):
        attempt_id = SUBMITTED["request"]["path"].rsplit("/", 1)[1]
        attempt = machine.project_attempt(SUBMITTED["response"]["body"], attempt_id=attempt_id,
                                          expected_plan_digest=AUTO["response"]["body"]["plan_digest"])
        self.assertEqual((attempt["resources"]["qos"], attempt["resources"]["walltime"]), ("owner100d", "01:00:00"))
        self.assertIn("QOS:        owner100d", "\n".join(machine_output.render_attempt(attempt)))

    def test_unrecognised_and_malformed_values(self):
        body = copy.deepcopy(AUTO["response"]["body"])
        body["resources"]["qos"] = "Some Other QOS"
        body["resource_selection"]["walltime"]["basis"] = "a guess"
        body["resource_selection"]["sources"]["cpus"] = "decided by text"
        plan = project(AUTO, body)
        self.assertEqual((plan["resources"]["qos"], plan["resource_selection"]["walltime"]["basis"],
                          plan["resource_selection"]["sources"]["cpus"]), (None, None, None))
        for mutate in (lambda b: b.update(plan_digest_version=3),
                       lambda b: b["resource_selection"].update(sources=["cpus"]),
                       lambda b: b["resource_selection"]["walltime"].update(recommended="soon")):
            body = copy.deepcopy(AUTO["response"]["body"])
            mutate(body)
            with self.assertRaises(UnexpectedResponse):
                project(AUTO, body)


class RunAndBatch(BatchBase):
    def setUp(self):
        super().setUp()
        self.compute.serve_plans("plan_si_energy_automatic", "plan_si_energy_partial", "plan_si_energy_manual_2400h")

    def sent_plans(self):
        return [json.loads(r["body"]) for r in self.compute.requests if r["method"] == "POST"]

    def test_omitted_resources_are_not_sent_and_explicit_ones_are_unchanged(self):
        code, out, err = self.run_cli(self.si, "--json")
        self.assertEqual(code, 0, err)
        document = json.loads(out)
        self.assertEqual(document["result"]["plan"]["plan_digest_version"], 2)
        self.assertEqual(document["result"]["plan"]["plan_digest"], AUTO["response"]["body"]["plan_digest"])
        self.assertEqual(self.sent_plans(), [AUTO["request"]["body"]])
        self.assertNotIn("resources", self.puts()[0])
        self.assertEqual(self.puts()[0]["expected_plan_digest"], AUTO["response"]["body"]["plan_digest"])
        self.run_cli(self.si, "--cpus", "48")
        code, out, err = self.run_cli(self.si, "--cpus", "48", "--memory-gb", "64", "--walltime", "2400:00:00", "--json")
        self.assertEqual(code, 0, err)  # run_cli validates the JSON against the published run-output schema
        self.assertEqual(json.loads(out)["request"]["resources"]["walltime"], "2400:00:00")
        self.assertEqual(self.sent_plans()[1:], [PARTIAL["request"]["body"], MANUAL["request"]["body"]])
        self.assertEqual(self.compute.sbatch_calls, 3)

    def test_api_plan_json_for_a_2400_hour_request_matches_the_schema(self):
        environ = {"HOME": str(self.ws.root), "BMD_RUN_STATE_DIR": str(self.ws.state), "BMD_RUN_API_URL": self.server.url}
        import io

        from bmd_run import cli
        out = io.StringIO()
        code = cli.main(["--json", "api", "plan", self.si, "--desired-output", "energy_only", "--cpus", "48",
                         "--memory-gb", "64", "--walltime", "2400:00:00", "--token-file", str(self.token_file)],
                        environ=environ, stdout=out, stderr=io.StringIO())
        document = json.loads(out.getvalue())
        self.assertEqual(code, 0, document["error"])
        self.assertEqual(schema_errors(document, MACHINE_SCHEMA), [])
        self.assertEqual((document["request"]["resources"]["walltime"], document["result"]["resources"]["walltime"]),
                         ("2400:00:00", "2400:00:00"))

    def test_human_output_shows_the_resolved_resources(self):
        code, out, _ = self.run_cli(self.si)
        self.assertEqual(code, 0)
        self.assertIn("Resources:    1 node(s), 24 CPUs, 128 GB, walltime 01:00:00, QOS owner100d", out)
        self.assertIn("Selection:    automatic: CPUs, memory, walltime", out)
        self.assertNotIn(EXPLANATION, out)

    def test_batch_cells_without_resources_are_all_planned_and_submitted(self):
        si = {"structure": "structures/Si.POSCAR", "desired_output": "energy_only"}
        manifest = self.manifest([{**si, "cell": "auto"}, {**si, "cell": "cpus", "cpus": 48},
                                  {**si, "cell": "manual", "cpus": 48, "memory_gb": 64, "walltime": "2400:00:00"}])
        code, document, err = self.batch(manifest)
        self.assertEqual(code, 0, err)
        self.assertEqual([r["action"] for r in document["cells"]], ["submitted"] * 3)
        self.assertEqual(self.sent_plans(), [c["request"]["body"] for c in (AUTO, PARTIAL, MANUAL)])
        code, document, _ = self.batch("status", manifest, "--local")
        auto, cpus, manual = document["cells"]
        self.assertEqual(auto["resources"], {
            "nodes": 1, "cpus": 24, "requested_memory_gb": 128, "walltime_limit": "01:00:00",
            "partition": "leeburton-pool", "account": "power-leeburton-users_v2", "qos": "owner100d",
            "sources": {"cpus": "automatic", "memory_gb": "automatic", "walltime": "automatic", "queue": "automatic"},
        })
        self.assertEqual(cpus["resources"]["sources"]["cpus"], "manual")
        self.assertEqual((manual["resources"]["walltime_limit"], manual["plan_digest"]),
                         ("2400:00:00", MANUAL["response"]["body"]["plan_digest"]))
        self.assertEqual(self.compute.sbatch_calls, 3)

    def test_campaigns_recorded_under_digest_v1_still_resume(self):
        # Recorded with the pre-R1 Compute (digest v1, no QOS), then read and resumed under R1.
        self.compute.serve_plans("plan_si_poscar_energy_only")
        manifest = self.manifest([{"cell": "old", "structure": "structures/Si.POSCAR", "desired_output": "energy_only"}])
        code, document, _ = self.batch(manifest)
        self.assertEqual((code, "qos" in document["cells"][0]["resources"]), (0, False))
        self.compute.serve_plans("plan_si_energy_automatic")
        code, document, _ = self.batch(manifest)
        self.assertEqual((code, document["cells"][0]["action"]), (0, "already_submitted"))
        code, document, _ = self.batch("status", manifest)
        self.assertEqual((code, document["cells"][0]["category"]), (0, "submitted"))


if __name__ == "__main__":
    unittest.main()
