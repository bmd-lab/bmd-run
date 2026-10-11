"""R3A: ``bmd-run batch MANIFEST`` and ``bmd-run batch status MANIFEST``.

In-process against the fake BMD Compute (``_api_support``) over real HTTP on
127.0.0.1, with temporary state directories. Nothing contacts a live VM or POWER.
"""

from __future__ import annotations

import copy
import io
import json
import os
import shutil
import unittest

from _api_support import fixture
from _support import ROOT, schema_errors
from test_run_command import RunBase

from bmd_run import batch, cli, errors
from bmd_run.api_transport import ApiTransport
from bmd_run.attempt_store import CampaignStore, new_cell
from bmd_run.errors import LocalStateError

BATCH_SCHEMA = json.loads((ROOT / "src" / "bmd_run" / "schemas" / "batch-output-v1.schema.json").read_text(encoding="utf-8"))
SI_PLAN = fixture("plan_si_poscar_energy_only")
COMPLETED = fixture("attempt_get_labelled_completed")["response"]["body"]["scheduler"]
NEVER_RELAYED = "Compute message that the client must never relay."

SI = {"cell": "si-energy", "structure": "structures/Si.POSCAR", "desired_output": "energy"}
NIO = {"cell": "nio-dos", "structure": "structures/NiO.POSCAR", "desired_output": "electronic_dos"}
CIF = {"cell": "si-cif", "structure": "structures/Si.cif", "desired_output": "energy_only"}
RES = {"cell": "si-48", "structure": "structures/Si.POSCAR", "desired_output": "energy_only",
       "cpus": 48, "memory_gb": 64, "walltime": "12:00:00"}
CUSTOM = {"cell": "si-custom", "structure": "structures/Si.POSCAR", "custom_workflow": "relax_static.json"}


class BatchBase(RunBase):
    def setUp(self):
        super().setUp()
        self.campaign_dir = self.ws.root / "campaign"
        (self.campaign_dir / "structures").mkdir(parents=True)
        for name in ("Si.POSCAR", "NiO.POSCAR", "Si.cif"):
            shutil.copy2(self.ws.files / name, self.campaign_dir / "structures" / name)
        (self.campaign_dir / "relax_static.json").write_text(json.dumps(
            {"stages": [{"stage_type": "relax", "theory": "pbe"}, {"stage_type": "static", "theory": "pbe"}]}),
            encoding="utf-8")
        self.store = CampaignStore(self.ws.state)

    def manifest(self, calculations, campaign="bench-1", name="benchmark.json", **extra):
        path = self.campaign_dir / name
        path.write_text(json.dumps({"version": 1, "campaign": campaign, "calculations": calculations, **extra}),
                        encoding="utf-8")
        return str(path)

    def batch(self, *argv, url=None, factory=None, timeout="5"):
        """Run ``bmd-run batch ...`` with --json; returns (code, document, stderr)."""

        code, out, err = self.run_cli("batch", *argv, "--json", url=url, factory=factory, timeout=timeout,
                                      run_schema=False)
        document = json.loads(out)
        self.assertEqual(schema_errors(document, BATCH_SCHEMA), [])
        self.assertNotIn(NEVER_RELAYED, out + err)
        return code, document, err

    def text(self, *argv, url=None):
        code, out, err = self.run_cli("batch", *argv, url=url, run_schema=False)
        self.assertNotIn(NEVER_RELAYED, out + err)
        return code, out, err

    def cells(self, document):
        return {row["cell"]: row for row in document["cells"]}

    def submits(self, attempt_id):
        return [json.loads(r["body"])["submit"] for r in self.compute.requests
                if r["method"] == "PUT" and r["path"].endswith(attempt_id)]

    def interrupting(self, predicate):
        class Interrupting(ApiTransport):
            def call(inner, endpoint, *, attempt_id=None, body=None):
                if predicate(endpoint, body):
                    raise KeyboardInterrupt
                return ApiTransport.call(inner, endpoint, attempt_id=attempt_id, body=body)
        return Interrupting

    def assert_one_attempt_per_cell(self):
        records = self.ws.records()
        directory = self.ws.state / "campaigns"
        cells = [json.loads(p.read_text(encoding="utf-8")) for p in directory.rglob("*.json")] if directory.exists() else []
        ids = [cell["attempt_id"] for cell in cells]
        self.assertEqual(len(ids), len(set(ids)), "an attempt is bound to one cell only")
        self.assertTrue(set(records) <= set(ids), "every attempt record belongs to a cell")
        self.assertTrue(set(self.compute.ledger) <= set(ids), "every Compute attempt belongs to a cell")


class Manifest(BatchBase):
    def test_invalid_manifests_send_nothing(self):
        def raw(text):
            path = self.campaign_dir / "raw.json"
            path.write_text(text, encoding="utf-8")
            return str(path)

        cases = [
            raw("not json"),
            raw("[]"),
            raw(json.dumps({"campaign": "c", "calculations": [SI]})),
            raw(json.dumps({"version": 2, "campaign": "c", "calculations": [SI]})),
            raw(json.dumps({"version": True, "campaign": "c", "calculations": [SI]})),
            raw(json.dumps({"version": 1, "campaign": "c", "calculations": [SI], "extra": 1})),
            self.manifest([SI], campaign="../escape"),
            self.manifest([SI], campaign=""),
            self.manifest([]),
            self.manifest([SI] * (batch.MAX_CALCULATIONS + 1)),
            self.manifest([{**SI, "cell": "a/b"}]),
            self.manifest([{**SI, "cell": ""}]),
            self.manifest([{k: v for k, v in SI.items() if k != "cell"}]),
            self.manifest([{k: v for k, v in SI.items() if k != "structure"}]),
            self.manifest([{k: v for k, v in SI.items() if k != "desired_output"}]),
            self.manifest([{**SI, "custom_workflow": "relax_static.json"}]),
            self.manifest([{**SI, "cpus": "24"}]), self.manifest([{**SI, "cpus": True}]),
            self.manifest([{**SI, "memory_gb": 0}]), self.manifest([{**SI, "walltime": "2 days"}]),
            self.manifest([{**SI, "format": "xyz"}]), self.manifest([{**SI, "incar": {"ENCUT": 520}}]),
            self.manifest([{**SI, "structure": "structures/missing.vasp"}]),
            self.manifest([{**SI, "desired_output": "Energy Only"}]),
            self.manifest([CUSTOM | {"custom_workflow": "missing.json"}]),
            str(self.campaign_dir / "missing-manifest.json"),
        ]
        for path in cases:
            code, document, _ = self.batch(path)
            self.assertEqual(code, errors.EXIT_USAGE, path)
            self.assertEqual(document["cells"], [])
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.ws.state / "campaigns").exists())

    def test_duplicate_cell_identifiers_are_refused(self):
        for cells in ([SI, {**NIO, "cell": SI["cell"]}], [SI, {**NIO, "cell": SI["cell"].upper()}]):
            code, document, _ = self.batch(self.manifest(cells))
            self.assertEqual(code, errors.EXIT_USAGE)
            self.assertIn("already used", document["error"]["message"])
        self.assertEqual(self.calls(), [])

    def test_errors_name_the_calculation(self):
        code, document, _ = self.batch(self.manifest([SI, {**NIO, "structure": "structures/none.vasp"}]))
        self.assertEqual(code, errors.EXIT_USAGE)
        self.assertIn("Calculation 2 (nio-dos)", document["error"]["message"])

    def test_paths_are_relative_to_the_manifest_and_requests_match_the_single_run_builder(self):
        manifest = self.manifest([SI, NIO, CIF, RES, CUSTOM])
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.ws.files)  # a different directory with files of the same names
        code, document, err = self.batch(manifest, "--max-new", "5")
        self.assertEqual(code, 0, err)
        sent = [json.loads(r["body"]) for r in self.compute.requests if r["method"] == "POST"]
        expected = ["plan_si_poscar_energy_only", "plan_nio_poscar_electronic_dos", "plan_si_cif_energy_only",
                    "plan_si_energy_only_resources", "plan_si_custom_relax_static"]
        self.assertEqual(sent, [fixture(name)["request"]["body"] for name in expected])
        self.assertEqual(self.compute.sbatch_calls, 5)
        # The same request through 'bmd-run STRUCTURE' and 'bmd-run api plan' is byte-identical.
        before = len(self.compute.requests)
        self.run_cli(str(self.campaign_dir / "structures" / "Si.POSCAR"), "--cpus", "48", "--memory-gb", "64",
                     "--walltime", "12:00:00")
        self.assertEqual(json.loads(self.compute.requests[before]["body"]), sent[3])

    def test_absolute_structure_paths_are_accepted(self):
        absolute = {**SI, "structure": str(self.campaign_dir / "structures" / "Si.POSCAR")}
        code, _, err = self.batch(self.manifest([absolute]))
        self.assertEqual(code, 0, err)


class Running(BatchBase):
    def test_default_starts_one_new_attempt_and_reruns_continue(self):
        manifest = self.manifest([SI, NIO, CIF])
        code, document, err = self.batch(manifest)
        self.assertEqual(code, 0, err)
        self.assertEqual(document["options"]["max_new"], 1)
        self.assertEqual([r["action"] for r in document["cells"]], ["submitted", "not_started", "not_started"])
        self.assertEqual(document["summary"], {"pending": 2, "submitted": 1, "completed": 0, "failed": 0,
                                               "uncertain": 0, "new_attempts": 1})
        self.assertEqual(self.compute.sbatch_calls, 1)
        self.assertEqual(sorted(p.name for p in (self.ws.state / "campaigns" / "bench-1").iterdir()),
                         ["si-energy.json"], "cells beyond the limit are not even reserved")
        first = self.cells(document)["si-energy"]["attempt_id"]
        self.assertIn(f"cell si-energy attempt {first} recorded", err)

        before = len(self.compute.requests)
        code, document, _ = self.batch(manifest)
        self.assertEqual([r["action"] for r in document["cells"]], ["already_submitted", "submitted", "not_started"])
        self.assertNotIn(first, "".join(r["path"] for r in self.compute.requests[before:]), "no request for a submitted cell")
        self.batch(manifest)
        before = len(self.compute.requests)
        code, document, _ = self.batch(manifest)
        self.assertEqual((code, [r["action"] for r in document["cells"]]), (0, ["already_submitted"] * 3))
        self.assertEqual(self.compute.requests[before:], [], "a finished batch sends nothing")
        self.assertEqual(self.compute.sbatch_calls, 3)
        self.assertEqual(len(self.compute.ledger), 3)
        self.assert_one_attempt_per_cell()

    def test_max_new_bounds(self):
        manifest = self.manifest([SI, NIO])
        for value in ("0", "21", "-1", "two", "1.5"):
            code, _, _ = self.batch(manifest, "--max-new", value)
            self.assertEqual(code, errors.EXIT_USAGE, value)
        self.assertEqual(self.calls(), [])
        code, document, _ = self.batch(manifest, "--max-new", "2")
        self.assertEqual((code, document["summary"]["new_attempts"], self.compute.sbatch_calls), (0, 2, 2))

    def test_cell_and_plan_are_recorded_before_any_remote_side_effect(self):
        seen = []

        def observe(method, path, body):
            cell = self.store.load_cell("bench-1", "si-energy")
            seen.append((method, path, copy.deepcopy(cell), dict(self.ws.records())))

        self.compute.observers.append(observe)
        code, document, _ = self.batch(self.manifest([SI]))
        self.assertEqual(code, 0)
        attempt = self.cells(document)["si-energy"]["attempt_id"]
        (m1, _, cell1, rec1), (m2, path2, cell2, rec2), _ = seen
        self.assertEqual(m1, "POST")
        self.assertEqual((cell1["attempt_id"], cell1["plan"], rec1), (attempt, None, {}))  # reserved before planning
        self.assertEqual((m2, path2), ("PUT", f"/api/v1/attempts/{attempt}"))
        self.assertEqual(cell2["plan"]["plan_digest"], SI_PLAN["response"]["body"]["plan_digest"])
        self.assertEqual(rec2[attempt]["local_state"], "planned")

    def test_text_output(self):
        code, out, _ = self.text(self.manifest([SI, NIO]))
        self.assertEqual(code, 0)
        self.assertIn("campaign bench-1, 2 calculations, at most 1 new attempt this run", out)
        self.assertIn("SLURM job 920001", out)
        self.assertIn("not started (new-attempt limit reached)", out)
        self.assertIn("bmd-run batch status", out)

    def test_changed_inputs_of_recorded_cells_are_refused_but_new_cells_may_be_added(self):
        manifest = self.manifest([SI])
        self.batch(manifest)
        with open(self.campaign_dir / "structures" / "Si.POSCAR", "a", encoding="utf-8") as handle:
            handle.write("\n")
        before = len(self.compute.requests)
        code, document, _ = self.batch(manifest)
        self.assertEqual(code, errors.EXIT_USAGE)
        self.assertIn("si-energy", document["error"]["message"])
        self.assertEqual(self.compute.requests[before:], [])
        shutil.copy2(self.ws.files / "Si.POSCAR", self.campaign_dir / "structures" / "Si.POSCAR")
        code, document, _ = self.batch(self.manifest([SI, NIO]))
        self.assertEqual([r["action"] for r in document["cells"]], ["already_submitted", "submitted"])

    def test_cells_are_bound_to_their_api_origin(self):
        manifest = self.manifest([SI, NIO])
        self.batch(manifest)
        code, document, _ = self.batch(manifest, url="http://127.0.0.1:9")
        self.assertEqual(code, errors.EXIT_USAGE)
        self.assertIn("different BMD Compute API origin", document["error"]["message"])

    def test_a_cell_rejected_by_compute_does_not_stop_the_others(self):
        (self.campaign_dir / "structures" / "bad.vasp").write_text("not a structure\n", encoding="utf-8")
        manifest = self.manifest([{**SI, "cell": "bad", "structure": "structures/bad.vasp"}, NIO])
        code, document, _ = self.batch(manifest, "--max-new", "2")
        self.assertEqual(code, errors.EXIT_INVALID_REQUEST)
        bad, nio = document["cells"]
        self.assertEqual((bad["action"], bad["category"], bad["last_error"]["stage"], bad["last_error"]["compute_code"]),
                         ("failed", "failed", "plan", "structure_invalid"))
        self.assertIsNone(bad["local_state"], "a rejected plan records no attempt")
        self.assertEqual((nio["action"], nio["category"]), ("submitted", "submitted"))
        self.assertEqual(len(self.compute.ledger), 1)

    def test_concurrent_reservation_of_a_cell_is_refused(self):
        record = new_cell(campaign="bench-1", cell="si-energy", attempt_id="15152671-4724-4962-8d4c-78425ed6b968",
                          api_origin=self.server.url, request_sha="0" * 64,
                          structure_source={"file_name": "Si.POSCAR", "sha256": "0" * 64, "format": "poscar"})
        self.store.create_cell(record)
        with self.assertRaises(LocalStateError):
            self.store.create_cell(record)

    def test_campaign_records_are_private_and_validated(self):
        self.batch(self.manifest([SI]))
        path = self.ws.state / "campaigns" / "bench-1" / "si-energy.json"
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o700)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["observed"]["scheduler"] = {"state": "<script>", "summary": "x", "elapsed": None, "exit_code": None,
                                             "started_at": None, "ended_at": None, "terminal": False}
        path.write_text(json.dumps(document), encoding="utf-8")
        code, document, _ = self.batch(self.manifest([SI]))
        self.assertEqual((code, document["error"]["kind"]), (errors.EXIT_LOCAL_STATE, "local_state_error"))


class Recovery(BatchBase):
    def test_interrupted_while_planning_reuses_the_reserved_attempt_id(self):
        manifest = self.manifest([SI])
        code, _, _ = self.batch(manifest, factory=self.interrupting(lambda e, b: e.name == "PLANS"))
        self.assertEqual(code, 130)
        reserved = self.store.load_cell("bench-1", "si-energy")["attempt_id"]
        self.assertEqual((self.ws.records(), self.calls()), ({}, []))
        code, document, _ = self.batch(manifest)
        self.assertEqual((code, self.cells(document)["si-energy"]["attempt_id"]), (0, reserved))
        self.assertEqual(list(self.compute.ledger), [reserved])

    def test_reserved_cells_count_against_the_new_attempt_limit(self):
        manifest = self.manifest([SI, NIO])
        built = batch.load_manifest(manifest, cli._api_request)
        for calc, attempt in zip(built.calculations, ("15152671-4724-4962-8d4c-78425ed6b968",
                                                      "6f1d2c3b-4a59-4e68-8b7a-9c0d1e2f3a4b")):
            self.store.create_cell(new_cell(campaign="bench-1", cell=calc.cell, attempt_id=attempt,
                                            api_origin=self.server.url, request_sha=calc.request_sha,
                                            structure_source=calc.source))  # as if a run stopped while planning
        code, document, _ = self.batch(manifest)
        self.assertEqual([r["action"] for r in document["cells"]], ["submitted", "not_started"])
        self.assertEqual(list(self.compute.ledger), ["15152671-4724-4962-8d4c-78425ed6b968"])

    def test_interrupted_before_prepare_resumes_the_same_attempt(self):
        manifest = self.manifest([SI])
        code, _, _ = self.batch(manifest, factory=self.interrupting(
            lambda e, b: e.name == "PUT_ATTEMPT" and b.get("submit") is False))
        self.assertEqual(code, 130)
        (attempt, record), = self.ws.records().items()
        self.assertEqual(record["local_state"], "planned")
        code, document, _ = self.batch(manifest)
        self.assertEqual((code, self.cells(document)["si-energy"]["action"]), (0, "submitted"))
        self.assertEqual(self.submits(attempt), [False, True])
        self.assertEqual(self.compute.sbatch_calls, 1)
        self.assert_one_attempt_per_cell()

    def test_interrupted_during_submit_is_uncertain_and_never_resubmitted(self):
        manifest = self.manifest([SI, NIO])
        code, _, _ = self.batch(manifest, factory=self.interrupting(
            lambda e, b: e.name == "PUT_ATTEMPT" and b.get("submit") is True))
        self.assertEqual(code, 130)
        (attempt, record), = self.ws.records().items()
        self.assertEqual(record["local_state"], "submit_unconfirmed")
        code, document, _ = self.batch(manifest)
        si = self.cells(document)["si-energy"]
        self.assertEqual((si["action"], si["category"]), ("uncertain_skipped", "uncertain"))
        self.assertEqual(si["recovery"], [f"bmd-run api status {attempt}", f"bmd-run api submit {attempt}"])
        self.assertEqual(self.cells(document)["nio-dos"]["action"], "submitted", "the batch continues past it")
        self.assertEqual(self.submits(attempt), [False], "the interrupted submit never left; it is not repeated")

    def test_lost_prepare_response_stops_and_resumes(self):
        manifest = self.manifest([SI, NIO])
        self.compute.add_fault(None)
        self.compute.add_fault("drop_after_processing")
        code, document, _ = self.batch(manifest, "--max-new", "2")
        self.assertEqual(code, errors.EXIT_SERVICE_UNAVAILABLE)
        self.assertEqual([r["action"] for r in document["cells"]], ["stopped", "not_reached"])
        self.assertEqual(self.cells(document)["si-energy"]["local_state"], "prepare_unconfirmed")
        self.assertEqual(len([r for r in self.compute.requests if r["method"] == "POST"]), 1, "nio-dos not planned")
        code, document, _ = self.batch(manifest, "--max-new", "2")
        self.assertEqual((code, [r["action"] for r in document["cells"]]), (0, ["submitted", "submitted"]))
        self.assertEqual(len(self.compute.ledger), 2)
        self.assertEqual(self.compute.sbatch_calls, 2)
        self.assert_one_attempt_per_cell()

    def test_active_job_quota_stops_the_batch_and_a_rerun_submits_the_same_attempt(self):
        manifest = self.manifest([SI, NIO])
        for fault in (None, None, "active_job_cap_exceeded"):
            self.compute.add_fault(fault)
        code, document, _ = self.batch(manifest, "--max-new", "2")
        self.assertEqual(code, errors.EXIT_QUOTA_EXCEEDED)
        si = self.cells(document)["si-energy"]
        self.assertEqual((si["action"], si["local_state"], si["last_error"]["compute_code"]),
                         ("stopped", "prepared", "active_job_cap_exceeded"))
        self.assertEqual(self.cells(document)["nio-dos"]["action"], "not_reached")
        self.assertEqual(document["summary"]["new_attempts"], 1)
        code, document, _ = self.batch(manifest, "--max-new", "2")
        self.assertEqual((code, [r["action"] for r in document["cells"]]), (0, ["submitted", "submitted"]))
        self.assertIsNone(self.cells(document)["si-energy"]["last_error"])
        self.assertEqual(self.submits(si["attempt_id"]), [False, True, True])
        self.assertEqual(self.compute.sbatch_calls, 2)
        self.assert_one_attempt_per_cell()

    def test_submission_quota_and_new_attempt_quota(self):
        manifest = self.manifest([SI])
        for fault in (None, None, "submission_cap_exceeded"):
            self.compute.add_fault(fault)
        code, _, _ = self.batch(manifest)
        self.assertEqual(code, errors.EXIT_QUOTA_EXCEEDED)
        self.assertEqual(self.compute.sbatch_calls, 0)
        manifest = self.manifest([NIO], campaign="bench-2", name="b2.json")
        for fault in (None, "attempt_cap_exceeded"):
            self.compute.add_fault(fault)
        code, document, _ = self.batch(manifest)
        self.assertEqual(code, errors.EXIT_QUOTA_EXCEEDED)
        nio = self.cells(document)["nio-dos"]
        self.assertEqual((nio["local_state"], nio["last_error"]["stage"]), ("planned", "prepare"))
        self.assertNotIn(nio["attempt_id"], self.compute.ledger)
        code, document, _ = self.batch(manifest)
        self.assertEqual((code, self.cells(document)["nio-dos"]["attempt_id"]), (0, nio["attempt_id"]))
        self.assert_one_attempt_per_cell()

    def test_uncertain_submission_stops_the_batch_and_is_never_retried(self):
        manifest = self.manifest([SI, NIO])
        for fault in (None, None, "timeout_after_processing"):
            self.compute.add_fault(fault)
        code, document, _ = self.batch(manifest, "--max-new", "2", timeout="0.5")
        self.assertEqual(code, errors.EXIT_SUBMISSION_UNCERTAIN)
        si = self.cells(document)["si-energy"]
        self.assertEqual((si["action"], si["category"]), ("stopped", "uncertain"))
        self.assertEqual(self.cells(document)["nio-dos"]["action"], "not_reached")
        code, document, _ = self.batch(manifest, "--max-new", "2")
        self.assertEqual([r["action"] for r in document["cells"]], ["uncertain_skipped", "submitted"])
        self.assertEqual(self.submits(si["attempt_id"]), [False, True])
        self.assertEqual(self.compute.sbatch_calls, 2)

    def test_compute_reported_uncertainty(self):
        manifest = self.manifest([SI])
        for fault in (None, None, "sbatch_uncertain"):
            self.compute.add_fault(fault)
        code, document, _ = self.batch(manifest)
        self.assertEqual((code, self.cells(document)["si-energy"]["local_state"]),
                         (errors.EXIT_SUBMISSION_UNCERTAIN, "submission_uncertain"))
        code, document, _ = self.batch(manifest)
        self.assertEqual(self.cells(document)["si-energy"]["action"], "uncertain_skipped")
        self.assertEqual(self.compute.sbatch_calls, 1)

    def test_a_changed_plan_is_not_silently_accepted(self):
        manifest = self.manifest([SI])
        code, _, _ = self.batch(manifest, factory=self.interrupting(
            lambda e, b: e.name == "PUT_ATTEMPT"))  # planned and recorded, then interrupted
        attempt = next(iter(self.ws.records()))
        # Simulate a crash before the attempt record was written: drop it, keep the cell's plan.
        os.replace(self.ws.state / "attempts" / f"{attempt}.json", self.ws.root / "moved.json")
        cell = self.store.load_cell("bench-1", "si-energy")
        cell["plan"]["plan_digest"] = "sha256:" + "1" * 64  # the plan the cell was bound to
        self.store.update_cell(cell)
        code, document, _ = self.batch(manifest)
        self.assertEqual(code, errors.EXIT_PLAN_DIGEST_MISMATCH)
        si = self.cells(document)["si-energy"]
        self.assertEqual((si["category"], si["plan_digest"]), ("failed", "sha256:" + "1" * 64))
        self.assertEqual(self.ws.records(), {})
        self.assertEqual(self.compute.ledger, {})


class Status(BatchBase):
    def test_local_status_before_any_run_needs_no_token_or_network(self):
        manifest = self.manifest([SI, NIO])
        code, document, _ = self.batch("status", manifest, "--local")
        self.assertEqual(code, 0)
        self.assertEqual([r["category"] for r in document["cells"]], ["pending", "pending"])
        self.assertEqual(document["options"], {"max_new": None, "refresh": False})
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.ws.state / "campaigns").exists(), "status creates no campaign")

    def test_local_status_after_a_run_sends_nothing(self):
        manifest = self.manifest([SI])
        self.batch(manifest)
        before = len(self.compute.requests)
        code, document, _ = self.batch("status", manifest, "--local")
        self.assertEqual((code, self.cells(document)["si-energy"]["category"]), (0, "submitted"))
        self.assertEqual(self.compute.requests[before:], [])
        self.assertIsNone(document["compute_api"]["origin"])

    def test_unrecognised_compute_values_are_not_relayed(self):
        manifest = self.manifest([SI])
        self.batch(manifest)
        (attempt,) = self.ws.records()
        secret = "Zq9xSecretScheduler"
        self.compute.ledger[attempt].update(scheduler={**COMPLETED, "state": secret, "summary": secret},
                                            scheduler_fixed=True)
        for argv in (["status", manifest], ["status", manifest, "--local"]):
            code, out, _ = self.run_cli("batch", *argv, "--json", run_schema=False)
            self.assertNotIn(secret, out + self.ws.all_state_text())
            self.assertEqual(json.loads(out)["cells"][0]["scheduler"]["state"], "UNKNOWN")

    def test_status_reads_only_and_reports_benchmark_fields(self):
        manifest = self.manifest([SI, NIO, CIF])
        self.batch(manifest, "--max-new", "2")
        before = len(self.compute.requests)
        code, document, _ = self.batch("status", manifest)
        self.assertEqual(code, 0)
        self.assertEqual({r["method"] for r in self.compute.requests[before:]}, {"GET"})
        si, nio, cif = document["cells"]
        self.assertEqual([si["category"], nio["category"], cif["category"]], ["submitted", "submitted", "pending"])
        plan = SI_PLAN["response"]["body"]
        self.assertEqual(si["plan_digest"], plan["plan_digest"])
        self.assertEqual(si["structure"], {"formula": "Si2", "reduced_formula": "Si", "natoms": 2,
                                           "space_group_number": 227, "crystal_system": "cubic"})
        self.assertEqual(si["workflow"], {"mode": "desired_output", "desired_output": "energy_only", "stage_count": 1,
                                          "stages": [{"stage_type": "static", "theory": "pbe", "modifiers": []}]})
        self.assertEqual(si["resources"], {"nodes": 1, "cpus": 24, "requested_memory_gb": 128,
                                           "walltime_limit": "72:00:00", "partition": "leeburton-pool",
                                           "account": "power-leeburton-users_v2"})
        self.assertEqual((si["job_id"], si["scheduler"]["state"], si["scheduler"]["elapsed"]), ("920001", "PENDING", "00:10:00"))
        self.assertEqual(nio["workflow"]["stage_count"], 3)
        self.assertIn("not measured peak memory", document["memory_note"])
        self.assertNotIn("memory_gb", json.dumps(document["cells"]).replace("requested_memory_gb", ""))

    def test_completed_failed_and_uncertain_are_classified(self):
        manifest = self.manifest([SI, NIO, CIF, RES])
        for fault in (None, None, None, None, None, None, None, None, None, None, None, "sbatch_uncertain"):
            self.compute.add_fault(fault)
        self.batch(manifest, "--max-new", "4")
        ids = {row["cell"]: row["attempt_id"] for row in self.batch("status", manifest, "--local")[1]["cells"]}
        done = self.compute.ledger[ids["si-energy"]]
        done.update(scheduler=dict(COMPLETED), scheduler_fixed=True)
        failed = self.compute.ledger[ids["nio-dos"]]
        failed.update(scheduler={**COMPLETED, "state": "OUT_OF_MEMORY", "summary": "FAILURE", "exit_code": "0:125"},
                      scheduler_fixed=True)
        code, document, _ = self.batch("status", manifest)
        rows = self.cells(document)
        self.assertEqual({cell: row["category"] for cell, row in rows.items()},
                         {"si-energy": "completed", "nio-dos": "failed", "si-cif": "submitted", "si-48": "uncertain"})
        self.assertEqual(rows["si-energy"]["scheduler"]["exit_code"], "0:0")
        self.assertEqual(rows["si-48"]["recovery"][0], f"bmd-run api status {ids['si-48']}")
        self.assertEqual(document["summary"], {"pending": 0, "submitted": 1, "completed": 1, "failed": 1,
                                               "uncertain": 1, "new_attempts": None})
        before = self.compute.sbatch_calls
        self.batch("status", manifest)
        self.assertEqual(self.compute.sbatch_calls, before)

    def test_status_resolves_an_unconfirmed_submission_without_resubmitting(self):
        manifest = self.manifest([SI])
        for fault in (None, None, "drop_after_processing"):
            self.compute.add_fault(fault)
        code, document, _ = self.batch(manifest)
        self.assertEqual(self.cells(document)["si-energy"]["category"], "uncertain")
        code, document, _ = self.batch("status", manifest)
        self.assertEqual(self.cells(document)["si-energy"]["category"], "submitted")
        self.assertEqual(self.compute.sbatch_calls, 1)

    def test_status_with_compute_unreachable_reports_local_records(self):
        manifest = self.manifest([SI, NIO])
        self.batch(manifest, "--max-new", "2")
        self.server.httpd.shutdown()
        self.server.httpd.server_close()
        code, document, _ = self.batch("status", manifest)
        self.assertEqual(code, errors.EXIT_SERVICE_UNAVAILABLE)
        self.assertEqual([r["category"] for r in document["cells"]], ["submitted", "submitted"])
        self.assertTrue(document["warnings"])

    def test_text_status_table(self):
        manifest = self.manifest([SI, NIO])
        self.batch(manifest)
        code, out, _ = self.text("status", manifest)
        self.assertEqual(code, 0)
        self.assertIn("REQ MEM GB", out)
        self.assertIn("not measured peak memory", out)
        self.assertRegex(out, r"si-energy\s+submitted\s+920001\s+PENDING\s+00:10:00\s+24\s+128\s+72:00:00")
        self.assertRegex(out, r"nio-dos\s+pending")


class Compatibility(BatchBase):
    def test_usage(self):
        for argv in ([], ["status"], ["a.json", "b.json"], ["status", "a.json", "b.json"]):
            code, document, _ = self.batch(*argv)
            self.assertEqual(code, errors.EXIT_USAGE, argv)
        manifest = self.manifest([SI])
        for argv in (["status", manifest, "--max-new", "2"], [manifest, "--local"]):
            self.assertEqual(self.batch(*argv)[0], errors.EXIT_USAGE, argv)
        self.assertEqual(self.calls(), [])

    def test_options_before_the_command_and_a_manifest_named_status(self):
        shutil.copy2(self.manifest([SI]), self.campaign_dir / "status")
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.campaign_dir)
        code, out, _ = self.run_cli("--json", "batch", "./status", run_schema=False)
        self.assertEqual((code, json.loads(out)["command"]), (0, "batch"))
        code, out, _ = self.run_cli("--json", "batch", "status", "./status", run_schema=False)
        self.assertEqual((code, json.loads(out)["command"]), (0, "batch_status"))

    def test_batch_help_does_not_run(self):
        out = io.StringIO()
        with self.assertRaises(SystemExit) as raised:
            import contextlib
            with contextlib.redirect_stdout(out):
                cli.main(["batch", "--help"], environ={}, stdout=out, stderr=io.StringIO())
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("bmd-run batch status MANIFEST", out.getvalue())
        self.assertEqual(self.calls(), [])

    def test_single_run_and_api_commands_are_unchanged(self):
        self.batch(self.manifest([SI]))
        code, out, _ = self.run_cli(str(self.ws.files / "Si.POSCAR"), "--json")
        self.assertEqual((code, json.loads(out)["schema"]), (0, "bmd_run.run_output"))
        attempt = json.loads(out)["attempt_id"]
        code, out, _ = self.run_cli("--json", "api", "status", attempt, run_schema=False)
        self.assertEqual((code, json.loads(out)["schema"]), (0, "bmd_run.machine_output"))
        self.assertEqual(len(list((self.ws.state / "campaigns" / "bench-1").glob("*.json"))), 1)

    def test_a_planning_only_token_records_no_submission(self):
        planner = self.ws.token_file(self.compute.issue("planner", ("plan", "read")), name="planner")
        code, out, _ = self.run_cli("batch", self.manifest([SI]), "--json", token_file=planner, run_schema=False)
        self.assertEqual(code, errors.EXIT_INSUFFICIENT_SCOPE)
        self.assertEqual(self.compute.sbatch_calls, 0)


if __name__ == "__main__":
    unittest.main()
