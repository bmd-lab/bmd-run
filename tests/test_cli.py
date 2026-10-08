"""End-to-end CLI behaviour against recorded BMD Compute v1.0.0 responses."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from _support import (
    FIXTURES,
    RecordingNetwork,
    fixture_text,
    fixture_tokens_and_uuids,
    load_schema,
    run_cli,
    schema_errors,
    v1_router,
)

from bmd_run import errors

SI_FILE = str(FIXTURES / "Si.POSCAR")
NIO_FILE = str(FIXTURES / "NiO.POSCAR")
SCHEMA = load_schema()


def parse(stdout):
    document = json.loads(stdout)
    problems = schema_errors(document, SCHEMA)
    assert not problems, problems
    return document


class Commands(unittest.TestCase):
    def test_identity_is_labelled_a_behavioural_fingerprint(self):
        code, out, err, network = run_cli(["identity"])
        self.assertEqual(code, errors.EXIT_OK)
        self.assertTrue(out.startswith("*** BEHAVIOURAL FINGERPRINT - NOT VERIFIED SOURCE IDENTITY ***"))
        self.assertIn("behaves like the reference", out)
        self.assertEqual(
            network.calls,
            [("GET", "/openapi.json"), ("POST", "/analyze"), ("POST", "/build-workflow")],
        )

    def test_identity_json(self):
        code, out, _, _ = run_cli(["--json", "identity"])
        document = parse(out)
        result = document["result"]
        self.assertEqual(result["kind"], "behavioural_fingerprint")
        self.assertIs(result["verified_source_identity"], False)
        self.assertIn("NOT VERIFIED SOURCE IDENTITY", result["statement"])
        self.assertTrue(result["matches_reference"])

    def test_identity_mismatch_and_strict(self):
        def changed(method, path, form):
            if path == "/openapi.json":
                data = json.loads(fixture_text("openapi.json"))
                data["paths"]["/api/v1/identity"] = {"get": {}}
                return 200, json.dumps(data)
            return v1_router(method, path, form)

        code, out, err, _ = run_cli(["--json", "identity"], RecordingNetwork(changed))
        document = parse(out)
        self.assertEqual(code, errors.EXIT_OK)
        self.assertFalse(document["result"]["matches_reference"])
        self.assertFalse(document["result"]["probes"]["openapi_surface"]["matches_reference"])
        self.assertTrue(document["warnings"])
        code, _, _, _ = run_cli(["identity", "--strict"], RecordingNetwork(changed))
        self.assertEqual(code, errors.EXIT_FINGERPRINT_MISMATCH)

    def test_options(self):
        code, out, _, network = run_cli(["options"])
        self.assertEqual(code, 0)
        self.assertIn("electronic_dos", out)
        self.assertEqual(network.calls, [("POST", "/analyze")])
        document = parse(run_cli(["options", "--json"])[1])
        self.assertEqual(
            [d["value"] for d in document["result"]["catalogue"]["desired_outputs"]][:4],
            ["energy_only", "relaxed_structure", "electronic_dos", "electronic_band_structure"],
        )

    def test_analyze(self):
        code, out, _, network = run_cli(["analyze", NIO_FILE])
        self.assertEqual(code, 0)
        self.assertIn("NiO", out)
        self.assertIn("DFT+U: applied automatically by BMD Compute", out)
        self.assertEqual(network.requests[0]["form"], {"structure": fixture_text("NiO.POSCAR"), "fmt": "poscar"})
        document = parse(run_cli(["--json", "analyze", NIO_FILE])[1])
        self.assertEqual(document["request"]["format"], "poscar")

    def test_plan_dos_shows_workflow_and_resources_first(self):
        code, out, _, network = run_cli(["plan", "dos", SI_FILE])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("RESOLVED WORKFLOW  (BMD Compute Desired Output: electronic_dos, 3 stages)"))
        self.assertEqual(
            lines[1:4],
            [
                "  1. PBE Geometry Optimisation  [vasp_std]",
                "  2. HSE06 Static Energy  [vasp_std]",
                "  3. HSE06 Density of States  [vasp_std]",
            ],
        )
        self.assertEqual(lines[5], "RESOURCES")
        self.assertLess(out.index("RESOURCES"), out.index("METHOD CONSIDERATIONS"))
        self.assertLess(out.index("Walltime:"), out.index("INCAR"))
        self.assertIn("GENERATED INPUTS (all stages)", out)
        self.assertIn("  Stage 3 - HSE06 Density of States", out)
        self.assertIn("Nothing was prepared or submitted", out)
        self.assertEqual(network.calls, [("POST", "/build-workflow")])
        self.assertEqual(network.requests[0]["form"]["workflow"], "electronic_dos")

    def test_plan_json_puts_resolved_workflow_and_resources_first(self):
        document = parse(run_cli(["plan", "dos", SI_FILE, "--json"])[1])
        result = document["result"]
        self.assertEqual(list(result)[:2], ["resolved_workflow", "resources"])
        self.assertEqual(
            [(s["theory"], s["stage_type"]) for s in result["resolved_workflow"]["stages"]],
            [("pbe", "relax"), ("hse06", "static"), ("hse06", "dos")],
        )
        self.assertEqual(result["resources"]["cpus"], 24)
        self.assertEqual(result["resources"]["memory_gb"], 128)
        self.assertEqual(result["resources"]["walltime"], "72:00:00")
        self.assertEqual([s["index"] for s in result["generated_inputs"]["stages"]], [1, 2, 3])
        self.assertIn("poscar_preview", result["unavailable_from_v1_machine_interface"])
        self.assertEqual(document["request"]["desired_output"], "electronic_dos")
        self.assertEqual(document["request"]["desired_output_as_typed"], "dos")

    def test_plan_all_desired_outputs(self):
        for alias, value, stages in [
            ("energy", "energy_only", 1),
            ("relax", "relaxed_structure", 2),
            ("bands", "electronic_band_structure", 3),
            ("electronic_dos", "electronic_dos", 3),
        ]:
            with self.subTest(alias=alias):
                document = parse(run_cli(["--json", "plan", alias, SI_FILE])[1])
                self.assertEqual(document["result"]["resolved_workflow"]["desired_output"], value)
                self.assertEqual(document["result"]["resolved_workflow"]["stage_count"], stages)

    def test_plan_passes_resources_through_for_compute_to_decide(self):
        code, out, _, network = run_cli(["--json", "plan", "energy", SI_FILE, "--cpus", "7"])
        document = parse(out)
        self.assertEqual(code, errors.EXIT_COMPUTE_REJECTED)
        self.assertEqual(network.requests[0]["form"]["cpus"], "7")
        self.assertEqual(document["error"]["kind"], "compute_rejected")
        self.assertEqual(document["error"]["http_status"], 400)
        self.assertEqual(document["error"]["compute_stage"], "calculation_validation")
        self.assertEqual(document["error"]["message"], "BMD Compute rejected the calculation request.")

    def test_unknown_desired_output_is_decided_by_compute(self):
        code, out, _, network = run_cli(["--json", "plan", "dso", SI_FILE])
        self.assertEqual(code, errors.EXIT_COMPUTE_REJECTED)
        self.assertEqual(network.requests[0]["form"]["workflow"], "dso")
        self.assertEqual(parse(out)["error"]["kind"], "compute_rejected")

    def test_invalid_structure_is_decided_by_compute(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "POSCAR"
            path.write_text("not a structure\n", encoding="utf-8")
            code, out, err, network = run_cli(["analyze", str(path)])
        self.assertEqual(code, errors.EXIT_COMPUTE_REJECTED)
        self.assertIn("BMD Compute rejected the structure.", err)
        self.assertIn("BMD Compute stage: structure_validation", err)
        self.assertEqual(network.calls, [("POST", "/analyze")])

    def test_compute_resolving_a_different_output_is_refused(self):
        def wrong(method, path, form):
            return 200, fixture_text("build_si_energy_only.html")

        code, out, _, _ = run_cli(["--json", "plan", "dos", SI_FILE], RecordingNetwork(wrong))
        self.assertEqual(code, errors.EXIT_UNEXPECTED_RESPONSE)
        self.assertIn("resolved a different Desired Output", parse(out)["error"]["message"])

    def test_server_error_is_unexpected_response(self):
        code, out, _, _ = run_cli(["--json", "plan", "dos", SI_FILE],
                                  RecordingNetwork(lambda m, p, f: (500, "Internal Server Error")))
        self.assertEqual(code, errors.EXIT_UNEXPECTED_RESPONSE)
        self.assertEqual(parse(out)["error"]["http_status"], 500)


class UsageAndShape(unittest.TestCase):
    def test_usage_errors_send_nothing(self):
        cases = [
            [],
            ["plan", "dos"],
            ["plan", "DOS!", SI_FILE],
            ["plan", "dos", SI_FILE, "--cpus", "24; rm -rf /"],
            ["plan", "dos", "/no/such/POSCAR"],
            ["analyze", SI_FILE, "--format", "xyz"],
            ["prepare", SI_FILE],
            ["submit", SI_FILE],
            ["monitor", "123"],
            ["resume", "123"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                code, out, _, network = run_cli(["--json"] + argv)
                self.assertEqual(code, errors.EXIT_USAGE)
                self.assertEqual(parse(out)["error"]["kind"], "usage_error")
                self.assertEqual(network.requests, [])

    def test_missing_or_bad_compute_url(self):
        code, out, _, network = run_cli(["--json", "options"], env_url=None)
        self.assertEqual(code, errors.EXIT_USAGE)
        self.assertIn("No BMD Compute URL", parse(out)["error"]["message"])
        code, out, _, network = run_cli(["--json", "--compute-url", "http://compute.test/submit", "options"])
        self.assertEqual(code, errors.EXIT_USAGE)
        self.assertEqual(network.requests, [])

    def test_unreachable(self):
        from bmd_run import cli
        from bmd_run.transport import Transport
        import io

        def refuse(origin, timeout):
            raise OSError("no route to host")

        stdout = io.StringIO()
        code = cli.main(
            ["--json", "--compute-url", "http://compute.test:8000", "options"],
            transport_factory=lambda url, timeout: Transport(url, timeout=timeout, connection_factory=refuse),
            stdout=stdout,
            stderr=io.StringIO(),
        )
        self.assertEqual(code, errors.EXIT_UNREACHABLE)
        self.assertEqual(parse(stdout.getvalue())["error"]["kind"], "compute_unreachable")

    def test_cif_format_is_inferred_from_the_file_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.cif"
            path.write_text("data_x\n", encoding="utf-8")
            _, _, _, network = run_cli(["analyze", str(path)])
        self.assertEqual(network.requests[0]["form"]["fmt"], "cif")

    def test_every_request_uses_only_ordinary_form_fields(self):
        network = RecordingNetwork()
        for argv in (["identity"], ["options"], ["analyze", SI_FILE],
                     ["plan", "bands", SI_FILE, "--cpus", "48", "--memory-gb", "64",
                      "--walltime", "02:00:00", "--queue", "leeburton-pool"]):
            run_cli(argv, network)
        allowed = {"structure", "fmt", "workflow", "cpus", "memory_gb", "walltime", "queue"}
        for request in network.requests:
            self.assertLessEqual(set(request["form"]), allowed)
            self.assertIn((request["method"], request["path"]),
                          {("GET", "/openapi.json"), ("POST", "/analyze"), ("POST", "/build-workflow")})

    def test_no_output_ever_contains_submission_identity_material(self):
        secrets = fixture_tokens_and_uuids()
        for argv in (["identity"], ["options"], ["analyze", NIO_FILE], ["plan", "dos", NIO_FILE],
                     ["plan", "bands", SI_FILE], ["plan", "relax", SI_FILE], ["plan", "energy", SI_FILE]):
            for mode in ([], ["--json"]):
                with self.subTest(argv=argv, mode=mode):
                    _, out, err, _ = run_cli(mode + argv)
                    for secret in secrets:
                        self.assertNotIn(secret, out + err)


if __name__ == "__main__":
    unittest.main()
