"""R2: the one-command interface ``bmd-run STRUCTURE`` (plan, record, prepare, submit).

Runs in-process against the fake BMD Compute over real HTTP on 127.0.0.1
(``_api_support``, responses recorded from bmd-compute e3fbb3b). Nothing contacts a
live VM or POWER.
"""

from __future__ import annotations

import io
import json
import os
import unittest

from _api_support import Workspace, fake_compute, fixture
from _support import ROOT, schema_errors

from bmd_run import cli, errors

RUN_SCHEMA = json.loads((ROOT / "src" / "bmd_run" / "schemas" / "run-output-v1.schema.json").read_text(encoding="utf-8"))
SI_DIGEST = fixture("plan_si_poscar_energy_only")["response"]["body"]["plan_digest"]
ALL_SCOPES = ("plan", "read", "prepare", "submit")


@unittest.skipUnless(os.name == "posix", "token files and attempt records need POSIX permissions")
class RunBase(unittest.TestCase):
    def setUp(self):
        self._cm = fake_compute()
        self.server = self._cm.__enter__()
        self.compute = self.server.compute
        self.ws = Workspace()
        self.token = self.compute.issue("runner", ALL_SCOPES)
        self.token_file = self.ws.token_file(self.token)
        self.si = str(self.ws.files / "Si.POSCAR")

    def tearDown(self):
        self._cm.__exit__(None, None, None)
        self.ws.close()

    def run_cli(self, *argv, token_file=None, timeout="5", factory=None, url=None, run_schema=True):
        environ = {"HOME": str(self.ws.root), "BMD_RUN_STATE_DIR": str(self.ws.state),
                   "BMD_RUN_API_URL": url or self.server.url}
        full = list(argv) + ["--token-file", str(token_file or self.token_file)]
        if timeout is not None:
            full += ["--timeout", timeout]
        out, err = io.StringIO(), io.StringIO()
        kwargs = {"api_transport_factory": factory} if factory is not None else {}
        code = cli.main(full, environ=environ, stdout=out, stderr=err, **kwargs)
        out, err = out.getvalue(), err.getvalue()
        for text in (out, err, self.ws.all_state_text()):
            self.assertNotIn(self.token, text)
            self.assertNotIn(self.token.rsplit(".", 1)[1], text)
        if "--json" in argv and run_schema:
            self.assertEqual(schema_errors(json.loads(out), RUN_SCHEMA), [])
        return code, out, err

    def calls(self):
        return [(r["method"], r["path"]) for r in self.compute.requests]

    def puts(self):
        return [json.loads(r["body"]) for r in self.compute.requests if r["method"] == "PUT"]


class OneCommand(RunBase):
    def test_default_is_energy_only_and_runs_plan_prepare_submit(self):
        code, out, err = self.run_cli(self.si)
        self.assertEqual(code, 0, err)
        (attempt, record), = self.ws.records().items()
        self.assertEqual(self.calls(), [("POST", "/api/v1/plans"),
                                        ("PUT", f"/api/v1/attempts/{attempt}"),
                                        ("PUT", f"/api/v1/attempts/{attempt}")])
        plan_body = json.loads(self.compute.requests[0]["body"])
        self.assertEqual(plan_body, fixture("plan_si_poscar_energy_only")["request"]["body"])
        prepare, submit = self.puts()
        self.assertEqual((prepare["submit"], submit["submit"]), (False, True))
        self.assertEqual({k: v for k, v in prepare.items() if k != "submit"},
                         {k: v for k, v in submit.items() if k != "submit"})
        self.assertEqual(prepare["expected_plan_digest"], SI_DIGEST)
        self.assertEqual(record["expected_plan_digest"], SI_DIGEST)
        self.assertEqual(record["local_state"], "submitted")
        self.assertEqual(self.compute.sbatch_calls, 1)
        for text in ("Workflow:     Energy only", f"Attempt:      {attempt}", "Submitted:    SLURM job 920001",
                     f"bmd-run api status {attempt}"):
            self.assertIn(text, out)

    def test_json_output(self):
        code, out, _ = self.run_cli(self.si, "--json")
        self.assertEqual(code, 0)
        document = json.loads(out)
        attempt = document["attempt_id"]
        self.assertEqual((document["ok"], document["stage"], document["command"]), (True, "complete", "run"))
        self.assertEqual(document["recovery"], [f"bmd-run api status {attempt}"])
        self.assertEqual(document["result"]["plan"]["plan_digest"], SI_DIGEST)
        self.assertEqual(document["result"]["prepared"]["state"], "prepared")
        self.assertEqual(document["result"]["submitted"]["submission"]["job_id"], "920001")
        self.assertEqual(document["request"]["desired_output"], "energy_only")

    def test_cif_custom_and_resource_overrides_are_passed_through_unchanged(self):
        custom = self.ws.root / "workflow.json"
        custom.write_text(json.dumps({"stages": [{"stage_type": "relax", "theory": "pbe"},
                                                 {"stage_type": "static", "theory": "pbe"}]}), encoding="utf-8")
        cases = [
            ([str(self.ws.files / "Si.cif"), "--desired-output", "energy"], "plan_si_cif_energy_only"),
            ([self.si, "--custom-workflow", str(custom)], "plan_si_custom_relax_static"),
            ([self.si, "--desired-output", "energy_only", "--cpus", "48", "--memory-gb", "64",
              "--walltime", "12:00:00"], "plan_si_energy_only_resources"),
        ]
        for argv, name in cases:
            before = len(self.compute.requests)
            code, out, err = self.run_cli(*argv)
            self.assertEqual(code, 0, (argv, err))
            sent = json.loads(self.compute.requests[before]["body"])
            self.assertEqual(sent, fixture(name)["request"]["body"], argv)
            self.assertEqual({k: v for k, v in self.puts()[-1].items() if k not in ("submit", "expected_plan_digest")}, sent)
        code, out, _ = self.run_cli(self.si, "--custom-workflow", str(custom))
        self.assertIn("Workflow:     Custom workflow", out)

    def test_desired_output_shortcuts_are_forwarded_as_compute_identifiers(self):
        for alias, identifier in (("energy", "energy_only"), ("relax", "relaxed_structure"),
                                  ("dos", "electronic_dos"), ("bands", "electronic_band_structure")):
            before = len(self.compute.requests)
            self.run_cli(self.si, "--desired-output", alias)
            self.assertEqual(json.loads(self.compute.requests[before]["body"])["workflow"],
                             {"desired_output": identifier}, alias)

    def test_conflicting_or_invalid_options_send_nothing(self):
        custom = self.ws.root / "c.json"
        custom.write_text('{"stages":[{"stage_type":"static","theory":"pbe"}]}', encoding="utf-8")
        for argv in ([self.si, "--desired-output", "energy", "--custom-workflow", str(custom)],
                     [self.si, "--cpus", "many"], [self.si, "--walltime", "2 days"],
                     [self.si, "--compute-url", "http://compute.test:8000"],
                     [str(self.ws.root / "missing.vasp")], [self.si, "extra-positional"]):
            code, _, err = self.run_cli(*argv)
            self.assertEqual(code, errors.EXIT_USAGE, argv)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.ws.records(), {})

    def test_structure_file_is_read_only(self):
        path = self.ws.files / "Si.POSCAR"
        before = (os.stat(path).st_mtime_ns, path.read_bytes())
        self.run_cli(self.si)
        self.assertEqual((os.stat(path).st_mtime_ns, path.read_bytes()), before)

    def test_attempt_is_recorded_before_preparation(self):
        seen = []
        self.compute.observers.append(lambda method, path, body: seen.append(dict(self.ws.records())) if method == "PUT" else None)
        self.run_cli(self.si)
        first_put = seen[0]
        (record,) = first_put.values()
        self.assertEqual(record["local_state"], "planned")
        self.assertEqual(record["expected_plan_digest"], SI_DIGEST)


class Failures(RunBase):
    def assert_no_attempt(self):
        self.assertEqual(self.ws.records(), {})
        self.assertNotIn("PUT", [m for m, _ in self.calls()])

    def test_failed_plan_creates_no_attempt(self):
        bad = self.ws.files / "bad.vasp"
        bad.write_text("not a structure\n", encoding="utf-8")
        code, out, _ = self.run_cli(str(bad), "--json")
        self.assertEqual(code, errors.EXIT_INVALID_REQUEST)
        document = json.loads(out)
        self.assertEqual((document["stage"], document["attempt_id"], document["recovery"]), ("plan", None, []))
        self.assert_no_attempt()

    def test_planning_only_token_creates_an_attempt_but_prepares_nothing(self):
        planner = self.ws.token_file(self.compute.issue("planner", ("plan", "read")), name="planner")
        code, out, err = self.run_cli(self.si, token_file=planner)
        self.assertEqual(code, errors.EXIT_INSUFFICIENT_SCOPE)
        (attempt, record), = self.ws.records().items()
        self.assertEqual(record["local_state"], "planned")
        self.assertIn(f"bmd-run api prepare --attempt {attempt}", err)
        self.assertEqual(self.compute.sbatch_calls, 0)

    def test_tunnel_down_before_planning(self):
        self.server.httpd.shutdown()
        self.server.httpd.server_close()
        code, _, err = self.run_cli(self.si)
        self.assertEqual(code, errors.EXIT_SERVICE_UNAVAILABLE)
        self.assertIn("nothing was sent", err)
        self.assertIn("No attempt was recorded", err)
        self.assertEqual(self.ws.records(), {})

    def test_definite_preparation_failure_stops_before_submit_and_recovers_with_r1(self):
        self.compute.add_fault(None)
        self.compute.add_fault("prepare_failed")
        code, out, _ = self.run_cli(self.si, "--json")
        self.assertEqual(code, errors.EXIT_REMOTE_OPERATION_FAILED)
        document = json.loads(out)
        attempt = document["attempt_id"]
        self.assertEqual(document["stage"], "prepare")
        self.assertEqual(document["recovery"][:2], [f"bmd-run api prepare --attempt {attempt}",
                                                    f"bmd-run api submit {attempt}"])
        self.assertNotIn("Retry the same command", document["error"]["suggestion"])
        self.assertEqual([b["submit"] for b in self.puts()], [False], "never submitted after a failed prepare")
        self.assertEqual(self.recover(["api", "prepare", "--attempt", attempt]), 0)
        self.assertEqual(self.recover(["api", "submit", attempt]), 0)
        self.assertEqual(self.compute.sbatch_calls, 1)
        self.assertEqual(list(self.ws.records()), [attempt])

    def recover(self, argv):
        environ = {"HOME": str(self.ws.root), "BMD_RUN_STATE_DIR": str(self.ws.state), "BMD_RUN_API_URL": self.server.url}
        return cli.main(argv + ["--token-file", str(self.token_file)], environ=environ,
                        stdout=io.StringIO(), stderr=io.StringIO())

    def test_lost_prepare_response_is_not_followed_by_submit(self):
        self.compute.add_fault(None)
        self.compute.add_fault("drop_after_processing")
        code, _, err = self.run_cli(self.si)
        self.assertEqual(code, errors.EXIT_SERVICE_UNAVAILABLE)
        (attempt, record), = self.ws.records().items()
        self.assertEqual(record["local_state"], "prepare_unconfirmed")
        self.assertEqual([b["submit"] for b in self.puts()], [False])
        self.assertIn(f"bmd-run api prepare --attempt {attempt}", err)
        self.assertEqual(self.recover(["api", "prepare", "--attempt", attempt]), 0)
        self.assertEqual(self.recover(["api", "submit", attempt]), 0)
        self.assertEqual(self.compute.sbatch_calls, 1)
        self.assertEqual(list(self.ws.records()), [attempt])

    def test_definite_submission_refusals_leave_the_attempt_prepared(self):
        for fault, exit_code in (("submit_failed", errors.EXIT_REMOTE_OPERATION_FAILED),
                                 ("active_job_cap_exceeded", errors.EXIT_QUOTA_EXCEEDED),
                                 ("remote_busy", errors.EXIT_SERVICE_UNAVAILABLE)):
            self.compute.add_fault(None)
            self.compute.add_fault(None)
            self.compute.add_fault(fault)
            before = set(self.ws.records())
            code, out, _ = self.run_cli(self.si, "--json")
            self.assertEqual(code, exit_code, fault)
            document = json.loads(out)
            attempt = document["attempt_id"]
            self.assertEqual(document["stage"], "submit")
            self.assertEqual(document["result"]["prepared"]["state"], "prepared")
            self.assertEqual(self.ws.records()[attempt]["local_state"], "prepared")
            self.assertEqual(set(self.ws.records()) - before, {attempt})
            self.assertNotIn("Retry the same command", document["error"]["suggestion"] or "")

    def test_preparer_token_stops_at_submission(self):
        preparer = self.ws.token_file(self.compute.issue("preparer", ("plan", "read", "prepare")), name="preparer")
        code, _, err = self.run_cli(self.si, token_file=preparer)
        self.assertEqual(code, errors.EXIT_INSUFFICIENT_SCOPE)
        (attempt, record), = self.ws.records().items()
        self.assertEqual((record["local_state"], record["submit_requested"]), ("prepared", False))
        self.assertIn("Stopped during: submit", err)
        self.assertEqual(self.compute.sbatch_calls, 0)


class Uncertainty(RunBase):
    def assert_single_submission_after_recovery(self, attempt):
        environ = {"HOME": str(self.ws.root), "BMD_RUN_STATE_DIR": str(self.ws.state), "BMD_RUN_API_URL": self.server.url}
        out = io.StringIO()
        code = cli.main(["--json", "api", "status", attempt, "--token-file", str(self.token_file)],
                        environ=environ, stdout=out, stderr=io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["result"]["attempt"]["state"], "submitted")
        code = cli.main(["api", "submit", attempt, "--token-file", str(self.token_file)],
                        environ=environ, stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(self.compute.sbatch_calls, 1)
        self.assertEqual(list(self.ws.records()), [attempt])
        self.assertEqual(len(self.compute.ledger), 1)

    def test_timed_out_submission_is_uncertain_and_never_retried(self):
        self.compute.add_fault(None)
        self.compute.add_fault(None)
        self.compute.add_fault("timeout_after_processing")
        code, out, _ = self.run_cli(self.si, "--json", timeout="0.5")
        self.assertEqual(code, errors.EXIT_SUBMISSION_UNCERTAIN)
        document = json.loads(out)
        attempt = document["attempt_id"]
        self.assertEqual((document["stage"], document["error"]["kind"]), ("submit", "submission_uncertain"))
        self.assertEqual(document["recovery"], [f"bmd-run api status {attempt}", f"bmd-run api submit {attempt}"])
        self.assertEqual([b["submit"] for b in self.puts()], [False, True], "no automatic retry")
        self.assertEqual(self.ws.records()[attempt]["local_state"], "submit_unconfirmed")
        self.assert_single_submission_after_recovery(attempt)

    def test_unreadable_submit_answer_is_uncertain(self):
        passthrough = lambda status, doc: (status, "application/json", json.dumps(doc).encode())  # noqa: E731
        self.compute.raw_override.extend([passthrough, passthrough,
                                          lambda status, doc: (500, "text/html", b"<html>error at /srv/x</html>")])
        code, out, err = self.run_cli(self.si)
        self.assertEqual(code, errors.EXIT_SUBMISSION_UNCERTAIN)
        self.assertNotIn("/srv/x", out + err)
        (attempt,) = self.ws.records()
        self.assertIn("may or may not have been submitted", err)
        self.assert_single_submission_after_recovery(attempt)

    def test_compute_reported_uncertainty_is_never_resubmitted(self):
        self.compute.add_fault(None)
        self.compute.add_fault(None)
        self.compute.add_fault("sbatch_uncertain")
        code, out, _ = self.run_cli(self.si, "--json")
        self.assertEqual(code, errors.EXIT_SUBMISSION_UNCERTAIN)
        self.assertEqual(len([b for b in self.puts() if b["submit"]]), 1)
        self.assertEqual(self.compute.sbatch_calls, 1)


class InterruptionAndDiscovery(RunBase):
    def interrupting_factory(self, when):
        from bmd_run.api_transport import ApiTransport

        class Interrupting(ApiTransport):
            def call(inner, endpoint, *, attempt_id=None, body=None):
                if body is not None and endpoint.name == "PUT_ATTEMPT" and body.get("submit") is when:
                    raise KeyboardInterrupt
                return ApiTransport.call(inner, endpoint, attempt_id=attempt_id, body=body)

        return Interrupting

    def test_interrupt_during_submit_reports_the_recorded_attempt(self):
        code, out, err = self.run_cli(self.si, factory=self.interrupting_factory(True))
        self.assertEqual(code, 130)
        (attempt, record), = self.ws.records().items()
        self.assertEqual(record["local_state"], "submit_unconfirmed")
        self.assertIn(f"Attempt:      {attempt}", out)
        self.assertIn(f"bmd-run api status {attempt}", err)
        self.assertEqual(self.compute.sbatch_calls, 0)

    def test_records_are_discoverable_without_the_printed_id(self):
        # JSON mode prints the ID to stderr as soon as it is recorded, and records are files
        # named <attempt id>.json in the documented state directory (readable with ls/cat).
        code, out, err = self.run_cli(self.si, "--json", factory=self.interrupting_factory(False))
        self.assertEqual(code, 130)
        (attempt, record), = self.ws.records().items()
        self.assertIn(f"bmd-run: attempt {attempt} recorded", err)
        self.assertTrue((self.ws.state / "attempts" / f"{attempt}.json").is_file())
        self.assertEqual(record["structure_source"]["file_name"], "Si.POSCAR")
        self.assertEqual(json.loads(out)["recovery"][0], f"bmd-run api prepare --attempt {attempt}")


class Dispatch(RunBase):
    def test_options_before_the_structure_and_command_names(self):
        code, out, _ = self.run_cli("--json", "--desired-output", "energy", self.si)
        self.assertEqual((code, json.loads(out)["command"]), (0, "run"))

    def test_command_like_words_never_start_a_calculation(self):
        for word in ("prepare", "submit", "status", "monitor", "resume", "run", "batch", "plan", "api"):
            (self.ws.root / word).write_text((self.ws.files / "Si.POSCAR").read_text(encoding="utf-8"), encoding="utf-8")
        cwd = os.getcwd()
        os.chdir(self.ws.root)
        try:
            for word in ("prepare", "submit", "status", "monitor", "resume", "run", "batch"):
                code, out, _ = self.run_cli("--json", word, run_schema=False)
                self.assertEqual(code, errors.EXIT_USAGE, word)
                self.assertEqual(json.loads(out)["schema"], "bmd_run.output", word)
        finally:
            os.chdir(cwd)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.ws.records(), {})

    def test_a_structure_named_like_a_command_needs_a_path(self):
        (self.ws.root / "plan").write_text((self.ws.files / "Si.POSCAR").read_text(encoding="utf-8"), encoding="utf-8")
        cwd = os.getcwd()
        os.chdir(self.ws.root)
        try:
            code, _, _ = self.run_cli("./plan")
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0)

    def test_legacy_and_api_commands_are_not_captured(self):
        from _support import FIXTURES, run_cli

        code, _, _, network = run_cli(["plan", "energy", str(FIXTURES / "Si.POSCAR")])
        self.assertEqual((code, network.calls), (0, [("POST", "/build-workflow")]))
        code, _, _, network = run_cli(["identity"])
        self.assertEqual(code, 0)
        environ = {"HOME": str(self.ws.root), "BMD_RUN_STATE_DIR": str(self.ws.state), "BMD_RUN_API_URL": self.server.url}
        out = io.StringIO()
        code = cli.main(["--json", "api", "plan", self.si, "--desired-output", "energy_only",
                         "--token-file", str(self.token_file)], environ=environ, stdout=out, stderr=io.StringIO())
        self.assertEqual((code, json.loads(out.getvalue())["schema"]), (0, "bmd_run.machine_output"))
        self.assertEqual(self.ws.records(), {})

    def test_only_machine_api_routes_are_used(self):
        self.run_cli(self.si)
        self.assertTrue(all(path == "/api/v1/plans" or path.startswith("/api/v1/attempts/") for _, path in self.calls()))


if __name__ == "__main__":
    unittest.main()
