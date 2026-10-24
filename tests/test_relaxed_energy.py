"""Compute's Relaxed Energy Desired Output (bmd-compute E1, main 228cc59).

``relaxed_energy`` is PBE Geometry Optimisation -> PBE Geometry Optimisation ->
PBE Static Energy, resolved entirely by Compute. Runner only recognises the
identifier, names it, and carries Compute's plan (digest, stages, resources)
through prepare and submit unchanged. ``energy_only`` stays the one-command
default and keeps its single-point meaning.

Fixtures are Compute's own responses: the machine API set
``tests/fixtures/compute_api_v1_relaxed_energy`` and the browser set
``tests/fixtures/compute_e1_browser`` (both recorded from 228cc59). Nothing
contacts a live VM or POWER.
"""

from __future__ import annotations

import copy
import hashlib
import json
import unittest

from _api_support import RELAXED_ENERGY_FIXTURES, fixture
from _support import (
    ROOT, SI, NIO, TOKEN_RE, UUID_RE, RecordingNetwork, load_schema, run_cli as run_v1_cli,
    schema_errors,
)
from test_batch import BatchBase

from bmd_run import api_vocabulary, cli, errors, machine, machine_output, v1_html
from bmd_run import v1_vocabulary
from bmd_run.errors import UnexpectedResponse

SCHEMAS = ROOT / "src" / "bmd_run" / "schemas"
MACHINE_SCHEMA = json.loads((SCHEMAS / "machine-output-v1.schema.json").read_text(encoding="utf-8"))
RUN_SCHEMA = json.loads((SCHEMAS / "run-output-v1.schema.json").read_text(encoding="utf-8"))
OUTPUT_SCHEMA = load_schema()
E1_BROWSER = ROOT / "tests" / "fixtures" / "compute_e1_browser"
E1_COMMIT = "228cc59502ebc118c3d4128cbce8652e04b88f72"

SI_RELAXED = fixture("plan_si_poscar_relaxed_energy")
NIO_RELAXED = fixture("plan_nio_poscar_relaxed_energy")
MANUAL = fixture("plan_si_relaxed_energy_manual")
ENERGY_ONLY_E1 = fixture("plan_si_poscar_energy_only_e1")
CUSTOM_SHAPED = fixture("plan_si_custom_relax_relax_static")
UNKNOWN = fixture("plan_error_unknown_relaxed_energies")
PREPARED = fixture("attempt_prepare_relaxed_energy")
SUBMITTED = fixture("attempt_submit_relaxed_energy")
THREE_STAGES = ["PBE Geometry Optimisation", "PBE Geometry Optimisation", "PBE Static Energy"]


def project(case, body=None):
    return machine.project_plan(body if body is not None else case["response"]["body"], case["request"]["body"])


def _enum_lists(node):
    """Every enum in a schema that names Desired Outputs."""

    found = []
    if isinstance(node, dict):
        if isinstance(node.get("enum"), list) and "energy_only" in node["enum"]:
            found.append(node["enum"])
        for value in node.values():
            found += _enum_lists(value)
    elif isinstance(node, list):
        for value in node:
            found += _enum_lists(value)
    return found


# ------------------------------------------------------------- 1. vocabularies


class Vocabulary(unittest.TestCase):
    def test_fixture_provenance(self):
        manifest = json.loads((RELAXED_ENERGY_FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["compute_commit"], E1_COMMIT)
        for name, digest in manifest["files"].items():
            data = (RELAXED_ENERGY_FIXTURES / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest, name)
            self.assertNotIn(b"bmdc1.", data)
        browser = json.loads((E1_BROWSER / "cases.json").read_text(encoding="utf-8"))
        self.assertEqual(browser["compute_commit"], E1_COMMIT)

    def test_relaxed_energy_is_recognised_by_both_vocabularies(self):
        for vocabulary in (api_vocabulary, v1_vocabulary):
            self.assertIn("relaxed_energy", vocabulary.DESIRED_OUTPUTS)
            self.assertIn("energy_only", vocabulary.DESIRED_OUTPUTS)
            self.assertEqual(len(set(vocabulary.DESIRED_OUTPUTS)), len(vocabulary.DESIRED_OUTPUTS))
        self.assertEqual(v1_vocabulary.DESIRED_OUTPUT_LABELS["relaxed_energy"], "Relaxed Energy")
        self.assertEqual(v1_vocabulary.DESIRED_OUTPUT_LABELS["energy_only"], "Energy")
        self.assertEqual(machine_output._WORKFLOW_NAMES["relaxed_energy"], "Relaxed Energy")
        self.assertEqual(machine_output._WORKFLOW_NAMES["energy_only"], "Energy only")

    def test_schemas_add_the_identifier_without_opening_their_enums(self):
        expected = {
            "machine-output-v1.schema.json": set(api_vocabulary.DESIRED_OUTPUTS),
            "run-output-v1.schema.json": set(api_vocabulary.DESIRED_OUTPUTS),
            "output-v3.schema.json": set(v1_vocabulary.DESIRED_OUTPUTS),
        }
        for name, members in expected.items():
            schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
            enums = _enum_lists(schema)
            self.assertTrue(enums, name)
            for enum in enums:
                self.assertEqual(set(enum), members, name)
                self.assertNotIn("relaxed_energies", enum)
            # Still versioned 1 / 3: an additive member, not a format change.
            self.assertIn(schema["properties"]["schema_version"]["const"], (1, 3))

    def test_one_command_default_and_aliases_are_unchanged(self):
        self.assertEqual(cli.DEFAULT_DESIRED_OUTPUT, "energy_only")
        self.assertEqual(cli.DESIRED_OUTPUT_ALIASES, {
            "energy": "energy_only", "relax": "relaxed_structure",
            "dos": "electronic_dos", "bands": "electronic_band_structure",
        })
        self.assertEqual(cli.DESIRED_OUTPUT_SHORTCUTS, {
            "--relax": "relaxed_structure", "--dos": "electronic_dos", "--bands": "electronic_band_structure",
        })
        self.assertEqual(cli.resolve_desired_output("relaxed_energy"), "relaxed_energy")
        self.assertEqual(cli.resolve_desired_output("energy"), "energy_only")


# ------------------------------------------------- 2-3. machine-API projection and output


class Projection(unittest.TestCase):
    def test_three_stage_plan_is_projected_as_compute_resolved_it(self):
        plan = project(SI_RELAXED)
        body = SI_RELAXED["response"]["body"]
        self.assertEqual(plan["workflow"]["desired_output"], "relaxed_energy")
        self.assertEqual(plan["workflow"]["stage_count"], 3)
        self.assertEqual([(s["stage_type"], s["theory"], s["modifiers"]) for s in plan["workflow"]["stages"]],
                         [("relax", "pbe", []), ("relax", "pbe", []), ("static", "pbe", [])])
        self.assertEqual(len(plan["scientific_inputs"]["stages"]), 3)
        self.assertEqual((plan["plan_digest"], plan["plan_digest_version"]), (body["plan_digest"], 2))
        self.assertEqual(plan["request"]["desired_output"], "relaxed_energy")
        self.assertEqual(plan["resources"]["walltime"], body["resources"]["walltime"])
        self.assertEqual(plan["resource_selection"]["sources"]["walltime"], "automatic")
        self.assertEqual([s["index"] for s in plan["resource_selection"]["walltime"]["stages"]], [1, 2, 3])

    def test_human_and_machine_output(self):
        plan = project(SI_RELAXED)
        text = machine_output.render_plan(plan)
        run_text = machine_output.render_run_plan(plan, "Si.POSCAR")
        self.assertIn("WORKFLOW (BMD Compute: relaxed_energy, 3 stages)", text)
        self.assertIn("Workflow:     Relaxed Energy", run_text)
        self.assertIn("Stages:       3 (" + "; ".join(THREE_STAGES) + ")", run_text)
        self.assertNotIn("Custom workflow", run_text)
        document = machine_output.envelope(command="plan", api_url=None, request=None, result=plan, error=None)
        self.assertEqual(schema_errors(document, MACHINE_SCHEMA), [])
        self.assertEqual(json.loads(machine_output.to_json(document))["result"]["workflow"]["desired_output"],
                         "relaxed_energy")
        request = {**machine.request_summary(SI_RELAXED["request"]["body"]),
                   "structure_source": {"file_name": "Si.POSCAR", "sha256": "0" * 64, "format": "poscar"}}
        run_document = machine_output.run_envelope(
            api_url=None, request=request, progress={"stage": "plan", "plan": plan, "attempt_id": None}, error=None)
        self.assertEqual(schema_errors(run_document, RUN_SCHEMA), [])

    def test_treatments_resolved_by_compute_are_shown(self):
        plan = project(NIO_RELAXED)
        self.assertEqual(plan["workflow"]["desired_output"], "relaxed_energy")
        self.assertEqual([s["modifiers"] for s in plan["workflow"]["stages"]], [["dft_u", "spin_polarized"]] * 3)
        run_text = machine_output.render_run_plan(plan, "NiO.POSCAR")
        self.assertEqual(run_text.count("+ DFT+U, Spin Polarised"), 3)

    def test_manual_resources_are_kept(self):
        plan = project(MANUAL)
        self.assertEqual((plan["resources"]["cpus"], plan["resources"]["memory_gb"], plan["resources"]["walltime"]),
                         (48, 64, "12:00:00"))
        self.assertEqual(plan["resource_selection"]["sources"],
                         {"cpus": "manual", "memory_gb": "manual", "walltime": "manual", "queue": "automatic"})
        self.assertEqual(plan["plan_digest"], MANUAL["response"]["body"]["plan_digest"])
        self.assertNotEqual(plan["plan_digest"], project(SI_RELAXED)["plan_digest"])

    def test_energy_only_keeps_its_meaning_on_the_e1_compute(self):
        plan = project(ENERGY_ONLY_E1)
        self.assertEqual(plan["workflow"]["desired_output"], "energy_only")
        self.assertEqual([s["stage_type"] for s in plan["workflow"]["stages"]], ["static"])
        self.assertIn("Workflow:     Energy only", machine_output.render_run_plan(plan, "Si.POSCAR"))
        older = project(fixture("plan_si_poscar_energy_only"))  # bmd-compute e3fbb3b, plan digest version 1
        self.assertEqual((older["workflow"]["desired_output"], older["plan_digest_version"]), ("energy_only", 1))

    def test_custom_workflow_shaped_like_relaxed_energy_stays_custom(self):
        plan = project(CUSTOM_SHAPED)
        self.assertIsNone(plan["workflow"]["desired_output"])
        self.assertEqual(plan["request"]["workflow_mode"], "custom")
        self.assertEqual([s["stage_type"] for s in plan["workflow"]["stages"]], ["relax", "relax", "static"])
        self.assertIn("Workflow:     Custom workflow", machine_output.render_run_plan(plan, "Si.POSCAR"))
        self.assertIn("WORKFLOW (BMD Compute: custom, 3 stages)", machine_output.render_plan(plan))
        # Same stages and science as Relaxed Energy on Si, but a different, Custom plan.
        self.assertNotEqual(plan["plan_digest"], project(SI_RELAXED)["plan_digest"])

    def test_unknown_identifiers_are_still_not_reported(self):
        for recipe in ("relaxed_energies", "Relaxed Energy", "relaxed-energy", "RELAXED_ENERGY"):
            body = copy.deepcopy(SI_RELAXED["response"]["body"])
            body["workflow"]["recipe"] = recipe
            plan = project(SI_RELAXED, body)
            self.assertIsNone(plan["workflow"]["desired_output"], recipe)
            self.assertNotIn(recipe, machine_output.render_plan(plan) + machine_output.render_run_plan(plan, "X"))
        forged = machine_output.envelope(command="plan", api_url=None, request=None, result=project(SI_RELAXED),
                                         error=None)
        forged["result"]["workflow"]["desired_output"] = "relaxed_energies"
        self.assertNotEqual(schema_errors(forged, MACHINE_SCHEMA), [])
        self.assertEqual(UNKNOWN["response"]["status"], 422)

    def test_a_plan_for_another_request_is_refused(self):
        # Compute answering with a Relaxed Energy plan for an energy_only request is never accepted.
        with self.assertRaises(UnexpectedResponse):
            machine.project_plan(SI_RELAXED["response"]["body"], ENERGY_ONLY_E1["request"]["body"])

    def test_recorded_attempts_keep_the_plan_digest_and_resources(self):
        digest = SI_RELAXED["response"]["body"]["plan_digest"]
        attempt_id = SUBMITTED["request"]["path"].rsplit("/", 1)[1]
        for case, state in ((PREPARED, "prepared"), (SUBMITTED, "submitted")):
            self.assertEqual(case["request"]["body"]["workflow"], {"desired_output": "relaxed_energy"})
            self.assertEqual(case["request"]["body"]["expected_plan_digest"], digest)
            attempt = machine.project_attempt(case["response"]["body"], attempt_id=attempt_id,
                                              expected_plan_digest=digest)
            self.assertEqual((attempt["state"], attempt["plan_digest"]), (state, digest))
            self.assertEqual(attempt["resources"]["walltime"], SI_RELAXED["response"]["body"]["resources"]["walltime"])


# ------------------------------------------------ 6-7. one command, api and batch


class RunAndBatch(BatchBase):
    def setUp(self):
        super().setUp()
        self.compute.serve_plans("plan_si_poscar_relaxed_energy", "plan_nio_poscar_relaxed_energy",
                                 "plan_si_relaxed_energy_manual", "plan_si_poscar_energy_only_e1",
                                 "plan_si_custom_relax_relax_static", "plan_error_unknown_relaxed_energies")

    def posts(self):
        return [json.loads(r["body"]) for r in self.compute.requests if r["method"] == "POST"]

    def test_default_run_still_requests_energy_only(self):
        code, out, err = self.run_cli(self.si, "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.posts()[0]["workflow"], {"desired_output": "energy_only"})
        document = json.loads(out)
        self.assertEqual(document["result"]["plan"]["workflow"]["desired_output"], "energy_only")
        self.assertEqual(document["result"]["plan"]["plan_digest"], ENERGY_ONLY_E1["response"]["body"]["plan_digest"])

    def test_relaxed_energy_run_prepares_and_submits_the_approved_plan(self):
        code, out, err = self.run_cli(self.si, "--desired-output", "relaxed_energy", "--json")
        self.assertEqual(code, 0, err)  # run_cli validates against the published run-output schema
        document = json.loads(out)
        digest = SI_RELAXED["response"]["body"]["plan_digest"]
        self.assertEqual(self.posts(), [SI_RELAXED["request"]["body"]])
        puts = self.puts()
        self.assertEqual([body["submit"] for body in puts], [False, True])
        for body in puts:
            self.assertEqual(body["workflow"], {"desired_output": "relaxed_energy"})
            self.assertEqual(body["expected_plan_digest"], digest)
            self.assertNotIn("resources", body)
        result = document["result"]
        self.assertEqual(result["plan"]["workflow"]["desired_output"], "relaxed_energy")
        self.assertEqual((result["prepared"]["plan_digest"], result["submitted"]["plan_digest"]), (digest, digest))
        self.assertEqual(result["submitted"]["state"], "submitted")
        self.assertEqual(result["local"]["request"]["desired_output"], "relaxed_energy")
        self.assertEqual(self.compute.sbatch_calls, 1)
        code, out, _ = self.run_cli(self.si, "--desired-output", "relaxed_energy")
        self.assertEqual(code, 0)
        self.assertIn("Workflow:     Relaxed Energy", out)
        self.assertIn("Stages:       3 (" + "; ".join(THREE_STAGES) + ")", out)

    def test_manual_resources_are_sent_and_approved_unchanged(self):
        code, out, err = self.run_cli(self.si, "--desired-output", "relaxed_energy", "--cpus", "48",
                                      "--memory-gb", "64", "--walltime", "12:00:00", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.posts(), [MANUAL["request"]["body"]])
        for body in self.puts():
            self.assertEqual(body["resources"], {"cpus": 48, "memory_gb": 64, "walltime": "12:00:00"})
            self.assertEqual(body["expected_plan_digest"], MANUAL["response"]["body"]["plan_digest"])
        self.assertEqual(json.loads(out)["result"]["plan"]["resources"]["walltime"], "12:00:00")

    def test_custom_workflow_resembling_relaxed_energy_stays_custom(self):
        custom = self.campaign_dir / "rrs.json"
        custom.write_text(json.dumps({"stages": [{"stage_type": t, "theory": "pbe"} for t in ("relax", "relax", "static")]}),
                          encoding="utf-8")
        code, out, err = self.run_cli(self.si, "--custom", str(custom), "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.posts()[0]["workflow"]["custom"]["stages"][0]["stage_type"], "relax")
        plan = json.loads(out)["result"]["plan"]
        self.assertIsNone(plan["workflow"]["desired_output"])
        self.assertEqual(plan["plan_digest"], CUSTOM_SHAPED["response"]["body"]["plan_digest"])

    def test_unknown_desired_output_is_rejected_by_compute_and_nothing_is_prepared(self):
        code, out, err = self.run_cli(self.si, "--desired-output", "relaxed_energies", "--json")
        self.assertEqual(code, errors.EXIT_INVALID_REQUEST, err)
        self.assertEqual(self.puts(), [])
        self.assertEqual(self.compute.sbatch_calls, 0)

    def test_api_plan_command(self):
        import io

        environ = {"HOME": str(self.ws.root), "BMD_RUN_STATE_DIR": str(self.ws.state), "BMD_RUN_API_URL": self.server.url}
        out = io.StringIO()
        code = cli.main(["--json", "api", "plan", self.si, "--desired-output", "relaxed_energy",
                         "--token-file", str(self.token_file)], environ=environ, stdout=out, stderr=io.StringIO())
        document = json.loads(out.getvalue())
        self.assertEqual(code, 0, document["error"])
        self.assertEqual(schema_errors(document, MACHINE_SCHEMA), [])
        self.assertEqual(document["result"]["workflow"]["desired_output"], "relaxed_energy")
        self.assertEqual(document["request"]["desired_output"], "relaxed_energy")

    def test_batch_keeps_relaxed_energy(self):
        manifest = self.manifest([
            {"cell": "si-relaxed-energy", "structure": "structures/Si.POSCAR", "desired_output": "relaxed_energy"},
            {"cell": "si-energy", "structure": "structures/Si.POSCAR", "desired_output": "energy_only"},
        ])
        code, document, _ = self.batch(manifest)
        self.assertEqual(code, 0)
        rows = self.cells(document)
        self.assertEqual((rows["si-relaxed-energy"]["workflow"]["desired_output"], rows["si-relaxed-energy"]["plan_digest"]),
                         ("relaxed_energy", SI_RELAXED["response"]["body"]["plan_digest"]))
        self.assertEqual(rows["si-relaxed-energy"]["workflow"]["stage_count"], 3)
        self.assertEqual(rows["si-energy"]["workflow"]["desired_output"], "energy_only")
        code, document, _ = self.batch("status", manifest, "--local")
        self.assertEqual(self.cells(document)["si-relaxed-energy"]["workflow"]["desired_output"], "relaxed_energy")
        self.assert_one_attempt_per_cell()


# ------------------------------------------- 4-5. transitional commands on the E1 browser


def _e1_page(name):
    return (E1_BROWSER / name).read_text(encoding="utf-8")


def e1_router(method, path, form):
    """Answer like the recorded BMD Compute E1 browser (228cc59)."""

    structure = form.get("structure")
    name = {SI: "si", NIO: "nio"}.get(structure)
    if method == "POST" and path == "/analyze" and name == "si":
        return 200, _e1_page("analyze_si.html")
    if method == "POST" and path == "/build-workflow" and name is not None:
        candidate = E1_BROWSER / f"build_{name}_{form.get('workflow')}.html"
        if candidate.is_file():
            return 200, candidate.read_text(encoding="utf-8")
        return 400, _e1_page("build_si_unknown.html")
    raise AssertionError(f"unexpected request {method} {path} {form.get('workflow')}")


def run_e1(*argv):
    code, out, err, network = run_v1_cli(["--json", *argv], network=RecordingNetwork(e1_router))
    document = json.loads(out)
    assert not schema_errors(document, OUTPUT_SCHEMA), schema_errors(document, OUTPUT_SCHEMA)
    secrets = set()
    for page in E1_BROWSER.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        secrets.update(TOKEN_RE.findall(text))
        secrets.update(UUID_RE.findall(text))
    for secret in secrets:
        assert secret not in out + err, "Compute form material was relayed"
    return code, document, network


class TransitionalCommandsOnE1(unittest.TestCase):
    def setUp(self):
        self.si = str(E1_BROWSER / "Si.POSCAR")
        self.nio = str(E1_BROWSER / "NiO.POSCAR")

    def test_analyze_recognises_the_new_browser_default(self):
        code, document, _ = run_e1("analyze", self.si)
        self.assertEqual(code, errors.EXIT_OK, document["error"])
        workflow = document["result"]["default_workflow"]
        self.assertEqual(workflow["desired_output"], "relaxed_energy")
        self.assertEqual([s["label"] for s in workflow["stages"]], THREE_STAGES)

    def test_options_lists_relaxed_energy(self):
        code, document, _ = run_e1("options")
        self.assertEqual(code, errors.EXIT_OK, document["error"])
        catalogue = document["result"]["catalogue"]
        outputs = {item["value"]: item for item in catalogue["desired_outputs"]}
        self.assertEqual(list(outputs), ["relaxed_energy", "relaxed_structure", "electronic_dos",
                                         "electronic_band_structure", "custom"])
        self.assertEqual(outputs["relaxed_energy"]["label"], "Relaxed Energy")
        self.assertEqual([s["label"] for s in outputs["relaxed_energy"]["default_stages"]], THREE_STAGES)
        # The E1 menu offers phonons (outside the transitional vocabulary) and no longer energy_only.
        self.assertNotIn("energy_only", outputs)
        self.assertGreaterEqual(catalogue["unrecognized_entry_count"], 1)

    def test_plan_relaxed_energy(self):
        code, document, network = run_e1("plan", "relaxed_energy", self.nio)
        self.assertEqual(code, errors.EXIT_OK, document["error"])
        workflow = document["result"]["resolved_workflow"]
        self.assertEqual((workflow["desired_output"], workflow["stage_count"]), ("relaxed_energy", 3))
        self.assertEqual([s["modifiers"] for s in workflow["stages"]], [["dft_u", "spin_polarized"]] * 3)
        self.assertEqual(network.requests[-1]["form"]["workflow"], "relaxed_energy")

    def test_historical_energy_only_and_its_alias(self):
        for typed in ("energy_only", "energy"):
            code, document, network = run_e1("plan", typed, self.si)
            self.assertEqual(code, errors.EXIT_OK, document["error"])
            workflow = document["result"]["resolved_workflow"]
            self.assertEqual((workflow["desired_output"], [s["label"] for s in workflow["stages"]]),
                             ("energy_only", ["PBE Static Energy"]))
            self.assertEqual(network.requests[-1]["form"]["workflow"], "energy_only")

    def test_unknown_identifier_is_rejected(self):
        code, document, _ = run_e1("plan", "relaxed_energies", self.si)
        self.assertEqual(code, errors.EXIT_COMPUTE_REJECTED)
        self.assertIsNone(document["result"])

    def test_a_page_resolving_a_different_output_is_refused(self):
        # The E1 relaxed_energy page answered for an energy_only request.
        def router(method, path, form):
            return 200, _e1_page("build_si_relaxed_energy.html")

        code, out, _, _ = run_v1_cli(["--json", "plan", "energy_only", self.si], network=RecordingNetwork(router))
        self.assertEqual(code, errors.EXIT_UNEXPECTED_RESPONSE)
        self.assertIn("resolved a different Desired Output", json.loads(out)["error"]["message"])

    def test_unrecognised_recipes_on_a_page_still_fail_closed(self):
        page = _e1_page("analyze_si.html").replace("&#34;recipe&#34;: &#34;relaxed_energy&#34;",
                                                    "&#34;recipe&#34;: &#34;relaxed_energies&#34;")
        self.assertIn("relaxed_energies", page)
        with self.assertRaises(UnexpectedResponse):
            v1_html.Page(page).workflow()


if __name__ == "__main__":
    unittest.main()
