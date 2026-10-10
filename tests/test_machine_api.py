"""Milestone R1: the authenticated machine-API client against a fake BMD Compute over real HTTP.

Every test runs ``bmd-run api ...`` in-process against ``_api_support.FakeComputeServer``
(127.0.0.1, recorded e3fbb3b fixtures). Nothing contacts a live VM or POWER.
"""

from __future__ import annotations

import json
import os
import socket
import unittest

from _api_support import (
    API_FIXTURES,
    Workspace,
    fake_compute,
    fixture,
    mode_of,
    run_api,
)
from _support import ROOT, schema_errors

from bmd_run import errors

SCHEMA = json.loads((ROOT / "src" / "bmd_run" / "schemas" / "machine-output-v1.schema.json").read_text(encoding="utf-8"))
SI_DIGEST = fixture("plan_si_poscar_energy_only")["response"]["body"]["plan_digest"]
ALL_SCOPES = ("plan", "read", "prepare", "submit")


def parse(stdout: str) -> dict:
    document = json.loads(stdout)
    problems = schema_errors(document, SCHEMA)
    assert not problems, problems
    return document


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(os.name == "posix", "token files and attempt records need POSIX permissions")
class Base(unittest.TestCase):
    def setUp(self):
        self._server_cm = fake_compute()
        self.server = self._server_cm.__enter__()
        self.compute = self.server.compute
        self.ws = Workspace()
        self.token = self.compute.issue("runner", ALL_SCOPES)
        self.token_file = self.ws.token_file(self.token)
        self.si = str(self.ws.files / "Si.POSCAR")

    def tearDown(self):
        self._server_cm.__exit__(None, None, None)
        self.ws.close()

    def api(self, *argv, token_file=None, **kwargs):
        code, out, err = run_api(argv, server=self.server, workspace=self.ws,
                                 token_file=token_file or self.token_file, **kwargs)
        for text in (out, err, self.ws.all_state_text()):
            self.assertNotIn(self.token, text)
            self.assertNotIn(self.token.rsplit(".", 1)[1], text)
        if "--json" in argv:
            parse(out)
        return code, out, err

    def calls(self):
        return [(r["method"], r["path"]) for r in self.compute.requests]

    def bodies(self, method):
        return [json.loads(r["body"]) for r in self.compute.requests if r["method"] == method]

    def prepare(self, *extra):
        code, out, _ = self.api("--json", "prepare", self.si, "--desired-output", "energy_only", *extra)
        self.assertEqual(code, 0, out)
        return json.loads(out)["result"]["attempt"]["attempt_id"]


# =================================================================== requests


class StructureAndWorkflowRequests(Base):
    def test_poscar_desired_output_plan_sends_the_file_text_unchanged(self):
        before = (os.stat(self.si).st_mtime_ns, (self.ws.files / "Si.POSCAR").read_bytes())
        code, out, err = self.api("--json", "plan", self.si, "--desired-output", "energy_only")
        self.assertEqual(code, errors.EXIT_OK, err)
        self.assertEqual(self.calls(), [("POST", "/api/v1/plans")])
        body = self.bodies("POST")[0]
        self.assertEqual(body, {
            "structure": {"format": "poscar", "text": before[1].decode("utf-8")},
            "workflow": {"desired_output": "energy_only"},
        })
        self.assertEqual(body, fixture("plan_si_poscar_energy_only")["request"]["body"])
        result = json.loads(out)["result"]
        self.assertEqual(result["plan_digest"], SI_DIGEST)
        self.assertEqual(result["structure"]["formula"], "Si2")
        self.assertEqual(result["resources"]["partition"], "leeburton-pool")
        self.assertEqual((os.stat(self.si).st_mtime_ns, (self.ws.files / "Si.POSCAR").read_bytes()), before)
        self.assertEqual(self.ws.records(), {}, "planning records nothing")

    def test_cif_request_uses_the_cif_format_from_the_file_name(self):
        code, out, _ = self.api("--json", "plan", str(self.ws.files / "Si.cif"), "--desired-output", "energy_only")
        self.assertEqual(code, 0, out)
        body = self.bodies("POST")[0]
        self.assertEqual(body["structure"]["format"], "cif")
        self.assertEqual(body["structure"]["text"], (API_FIXTURES / "Si.cif").read_text(encoding="utf-8"))
        self.assertEqual(json.loads(out)["result"]["request"]["structure_format"], "cif")

    def test_desired_output_alias_and_resources(self):
        code, out, _ = self.api("--json", "plan", self.si, "--desired-output", "energy",
                                "--cpus", "48", "--memory-gb", "64", "--walltime", "12:00:00")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.bodies("POST")[0]["resources"], {"cpus": 48, "memory_gb": 64, "walltime": "12:00:00"})
        self.assertEqual(self.bodies("POST")[0]["workflow"], {"desired_output": "energy_only"})

    def test_nio_plan_reports_compute_owned_treatments(self):
        code, out, _ = self.api("--json", "plan", str(self.ws.files / "NiO.POSCAR"), "--desired-output", "dos")
        self.assertEqual(code, 0, out)
        result = json.loads(out)["result"]
        stage = result["workflow"]["stages"][0]
        self.assertEqual(stage["modifiers"], ["dft_u", "spin_polarized"])
        self.assertEqual(stage["dft_u"]["species"]["Ni"]["U"], 6.2)
        ids = [c["id"] for c in result["method_considerations"]["considerations"]]
        self.assertEqual(ids, ["spin.composition_screen", "dftu.mp_oxide_fluoride"])

    def test_custom_workflow_uses_computes_stage_schema(self):
        custom = self.ws.root / "custom.json"
        custom.write_text(json.dumps({"stages": [{"stage_type": "relax", "theory": "pbe"},
                                                 {"stage_type": "static", "theory": "pbe"}]}), encoding="utf-8")
        code, out, _ = self.api("--json", "plan", self.si, "--custom-workflow", str(custom))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.bodies("POST")[0]["workflow"], {"custom": {"stages": [
            {"stage_type": "relax", "theory": "pbe", "modifiers": [], "options": {}},
            {"stage_type": "static", "theory": "pbe", "modifiers": [], "options": {}},
        ]}})
        self.assertEqual(json.loads(out)["result"]["request"]["workflow_mode"], "custom")

    def test_custom_workflow_must_be_computes_schema_and_nothing_else(self):
        for document in (
            {"stages": [{"stage_type": "static", "theory": "pbe", "incar": {"ENCUT": 2000}}]},
            {"stages": [{"stage_type": "static"}]},
            {"stages": [], "recipe": "energy_only"},
            {"stages": [{"stage_type": "../static", "theory": "pbe"}]},
            [{"stage_type": "static", "theory": "pbe"}],
        ):
            custom = self.ws.root / "bad.json"
            custom.write_text(json.dumps(document), encoding="utf-8")
            code, _, _ = self.api("plan", self.si, "--custom-workflow", str(custom))
            self.assertEqual(code, errors.EXIT_USAGE, document)
        self.assertEqual(self.calls(), [])

    def test_exactly_one_workflow_kind_and_no_materials_project_ids(self):
        self.assertEqual(self.api("plan", self.si)[0], errors.EXIT_USAGE)
        custom = self.ws.root / "c.json"
        custom.write_text('{"stages":[{"stage_type":"static","theory":"pbe"}]}', encoding="utf-8")
        self.assertEqual(self.api("plan", self.si, "--desired-output", "energy_only", "--custom-workflow", str(custom))[0], errors.EXIT_USAGE)
        self.assertEqual(self.api("plan", "mp-149", "--desired-output", "energy_only")[0], errors.EXIT_USAGE)
        self.assertEqual(self.calls(), [])

    def test_invalid_structure_and_calculation_are_classified_without_compute_prose(self):
        bad = self.ws.files / "bad.POSCAR"
        bad.write_text("not a structure\n", encoding="utf-8")
        code, out, _ = self.api("--json", "plan", str(bad), "--desired-output", "energy_only")
        self.assertEqual(code, errors.EXIT_INVALID_REQUEST)
        error = json.loads(out)["error"]
        self.assertEqual((error["kind"], error["compute_code"]), ("invalid_request", "structure_invalid"))
        self.assertNotIn("POSCAR contains a valid element-symbol line", out)

        code, out, _ = self.api("--json", "plan", self.si, "--desired-output", "nonsense_output")
        self.assertEqual(code, errors.EXIT_INVALID_REQUEST)
        self.assertEqual(json.loads(out)["error"]["diagnostic_code"], "unknown_desired_output")
        self.assertNotIn("Unknown Desired Output", out)


# ============================================================ authentication


class AuthenticationAndScopes(Base):
    def test_unknown_token_is_an_authentication_failure(self):
        other = self.ws.token_file("bmdc1.0000000000000000." + "A" * 43, name="other")
        code, out, _ = self.api("--json", "plan", self.si, "--desired-output", "energy_only", token_file=other)
        self.assertEqual(code, errors.EXIT_AUTHENTICATION_FAILED)
        self.assertEqual(json.loads(out)["error"]["kind"], "authentication_failed")

    def test_the_token_travels_only_in_the_authorization_header(self):
        self.api("plan", self.si, "--desired-output", "energy_only")
        request = self.compute.requests[0]
        self.assertEqual(request["headers"]["Authorization"], f"Bearer {self.token}")
        self.assertNotIn("?", request["path"])
        self.assertNotIn(self.token.encode(), request["body"])

    def test_planning_only_token_cannot_prepare(self):
        planner = self.ws.token_file(self.compute.issue("planner", ("plan", "read")), name="planner")
        code, out, _ = self.api("--json", "prepare", self.si, "--desired-output", "energy_only", token_file=planner)
        self.assertEqual(code, errors.EXIT_INSUFFICIENT_SCOPE)
        self.assertIn("'prepare' scope", json.loads(out)["error"]["message"])
        self.assertEqual(self.compute.sbatch_calls, 0)
        (record,) = self.ws.records().values()
        self.assertEqual(record["local_state"], "planned")

    def test_prepare_scope_cannot_submit_and_the_record_is_unchanged(self):
        preparer = self.ws.token_file(self.compute.issue("preparer", ("plan", "read", "prepare")), name="preparer")
        code, out, _ = self.api("--json", "prepare", self.si, "--desired-output", "energy_only", token_file=preparer)
        self.assertEqual(code, 0)
        attempt = json.loads(out)["result"]["attempt"]["attempt_id"]
        code, _, _ = self.api("submit", attempt, token_file=preparer)
        self.assertEqual(code, errors.EXIT_INSUFFICIENT_SCOPE)
        record = self.ws.records()[attempt]
        self.assertEqual((record["local_state"], record["submit_requested"]), ("prepared", False))
        self.assertEqual(self.compute.sbatch_calls, 0)

    def test_missing_unsafe_or_malformed_token_sources(self):
        cases = [
            (self.ws.root / "absent", errors.EXIT_CREDENTIALS_UNAVAILABLE),
            (self.ws.token_file(self.token, name="open", mode=0o644), errors.EXIT_CREDENTIALS_UNAVAILABLE),
            (self.ws.token_file("not-a-token", name="junk"), errors.EXIT_CREDENTIALS_UNAVAILABLE),
        ]
        link = self.ws.root / "link"
        link.symlink_to(self.token_file)
        cases.append((link, errors.EXIT_CREDENTIALS_UNAVAILABLE))
        for path, expected in cases:
            code, _, err = self.api("plan", self.si, "--desired-output", "energy_only", token_file=path)
            self.assertEqual(code, expected, path.name)
            self.assertNotIn("not-a-token", err)
        self.assertEqual(self.calls(), [])

    def test_environment_token_is_supported_and_ambiguity_is_refused(self):
        code, _, _ = run_api(["plan", self.si, "--desired-output", "energy_only"], server=self.server,
                             workspace=self.ws, env={"BMD_RUN_API_TOKEN": self.token})
        self.assertEqual(code, 0)
        code, _, _ = run_api(["plan", self.si, "--desired-output", "energy_only"], server=self.server,
                             workspace=self.ws, token_file=self.token_file, env={"BMD_RUN_API_TOKEN": self.token})
        self.assertEqual(code, errors.EXIT_USAGE)

    def test_token_repr_is_redacted(self):
        from bmd_run.credentials import ApiToken

        token = ApiToken(self.token, "test")
        self.assertNotIn(self.token.rsplit(".", 1)[1], repr(token) + str(token))


class Transport(Base):
    def test_insecure_or_unreviewed_origins_are_refused_before_connecting(self):
        for url in ("http://localhost:18000", "http://10.0.0.5:18000", "http://compute.example:18000",
                    "https://compute.example", "http://127.0.0.1", "http://127.0.0.1:18000/api"):
            code, _, _ = self.api("plan", self.si, "--desired-output", "energy_only", "--api-url", url)
            self.assertEqual(code, errors.EXIT_USAGE, url)
        self.assertEqual(self.calls(), [])

    def test_reviewed_https_requires_the_explicit_setting(self):
        from bmd_run.api_transport import ApiTransport
        from bmd_run.credentials import ApiToken

        token = ApiToken(self.token, "test")
        with self.assertRaises(errors.UsageError):
            ApiTransport("https://compute.example", token, environ={})
        transport = ApiTransport("https://compute.example", token, environ={"BMD_RUN_API_ALLOW_REMOTE_HTTPS": "1"})
        self.assertEqual(transport.base_url, "https://compute.example")

    def test_closed_tunnel_means_nothing_was_sent(self):
        url = f"http://127.0.0.1:{free_port()}"
        code, out, _ = self.api("--json", "plan", self.si, "--desired-output", "energy_only", "--api-url", url)
        self.assertEqual(code, errors.EXIT_SERVICE_UNAVAILABLE)
        self.assertIn("nothing was sent", json.loads(out)["error"]["message"])

    def test_query_strings_and_unlisted_routes_cannot_be_requested(self):
        from bmd_run import api_endpoints
        from bmd_run.api_transport import ApiTransport
        from bmd_run.credentials import ApiToken

        api = ApiTransport(self.server.url, ApiToken(self.token, "test"), environ={})
        for bad in ("../plans", "6F1D2C3B-4A59-4E68-8B7A-9C0D1E2F3A4B", "6f1d2c3b-4a59-4e68-8b7a-9c0d1e2f3a4b?x=1",
                    "6f1d2c3b-4a59-4e68-cb7a-9c0d1e2f3a4b", None):
            with self.assertRaises(errors.UsageError):
                api.call(api_endpoints.ApiEndpoint.GET_ATTEMPT, attempt_id=bad)
        with self.assertRaises(errors.UsageError):
            api.call(api_endpoints.ApiEndpoint.PLANS, attempt_id="6f1d2c3b-4a59-4e68-8b7a-9c0d1e2f3a4b", body={})
        with self.assertRaises(errors.UsageError):
            api.call(api_endpoints.ApiEndpoint.GET_ATTEMPT, attempt_id="6f1d2c3b-4a59-4e68-8b7a-9c0d1e2f3a4b", body={})
        with self.assertRaises(errors.UsageError):
            api.call("plans", body={})
        with self.assertRaises(AttributeError):
            api_endpoints.ApiEndpoint.PLANS.path_template = "/x"
        self.assertEqual(self.calls(), [])

    def test_redirects_non_json_and_oversized_responses_are_refused(self):
        for override in (
            lambda status, doc: (302, "application/json", b"{}"),
            lambda status, doc: (200, "text/html", b"<html>login</html>"),
            lambda status, doc: (200, "application/json", b"{not json"),
            lambda status, doc: (200, "application/json", b" " * (8 * 1024 * 1024 + 10)),
        ):
            self.compute.raw_override.append(override)
            code, _, _ = self.api("plan", self.si, "--desired-output", "energy_only")
            self.assertEqual(code, errors.EXIT_UNEXPECTED_RESPONSE)


# ======================================================== attempts and resume


class PrepareSubmitStatus(Base):
    def test_record_is_persisted_before_the_prepare_request(self):
        seen = []

        def observe(method, path, body):
            if method == "PUT":
                seen.append(dict(self.ws.records()))

        self.compute.observers.append(observe)
        attempt = self.prepare()
        (snapshot,) = seen
        record = snapshot[attempt]
        self.assertEqual(record["local_state"], "planned")
        self.assertEqual(record["expected_plan_digest"], SI_DIGEST)
        self.assertEqual(record["api_origin"], self.server.url)
        self.assertEqual(record["request"], fixture("plan_si_poscar_energy_only")["request"]["body"])
        self.assertEqual(self.calls(), [("POST", "/api/v1/plans"), ("PUT", f"/api/v1/attempts/{attempt}")])
        self.assertEqual(self.bodies("PUT")[0]["submit"], False)
        self.assertEqual(self.ws.records()[attempt]["local_state"], "prepared")

    def test_state_is_private_and_holds_no_credentials_paths_or_identity_tokens(self):
        attempt = self.prepare()
        self.assertEqual(mode_of(self.ws.state), 0o700)
        self.assertEqual(mode_of(self.ws.state / "attempts"), 0o700)
        self.assertEqual(mode_of(self.ws.state / "attempts" / f"{attempt}.json"), 0o600)
        record = self.ws.records()[attempt]
        self.assertEqual(set(record), {
            "schema", "schema_version", "attempt_id", "api_origin", "created_at", "updated_at", "request",
            "request_sha256", "structure_source", "expected_plan_digest", "local_state", "submit_requested",
            "last_observed", "events",
        })
        self.assertEqual(record["structure_source"]["file_name"], "Si.POSCAR")
        text = self.ws.all_state_text().lower()
        for needle in ("bmdc1.", "bearer", "authorization", "ssh", "identity_token", str(self.ws.root).lower()):
            self.assertNotIn(needle, text)

    def test_prepare_then_submit_reuses_the_exact_request(self):
        attempt = self.prepare()
        code, out, _ = self.api("--json", "submit", attempt)
        self.assertEqual(code, 0, out)
        result = json.loads(out)["result"]
        self.assertEqual(result["attempt"]["state"], "submitted")
        self.assertEqual(result["attempt"]["submission"]["job_id"], "920001")
        prepare_body, submit_body = self.bodies("PUT")
        self.assertEqual(prepare_body["submit"], False)
        self.assertEqual(submit_body["submit"], True)
        self.assertEqual({k: v for k, v in submit_body.items() if k != "submit"},
                         {k: v for k, v in prepare_body.items() if k != "submit"})
        self.assertEqual(self.calls().count(("POST", "/api/v1/plans")), 1, "submit never re-plans")
        self.assertEqual(self.ws.records()[attempt]["local_state"], "submitted")

    def test_repeated_prepare_and_submit_are_idempotent(self):
        attempt = self.prepare()
        for _ in range(2):
            self.assertEqual(self.api("prepare", "--attempt", attempt)[0], 0)
        for _ in range(3):
            code, out, _ = self.api("--json", "submit", attempt)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["result"]["attempt"]["submission"]["job_id"], "920001")
        self.assertEqual(self.compute.sbatch_calls, 1)
        self.assertEqual(list(self.ws.records()), [attempt])
        self.assertEqual(len({json.dumps({k: v for k, v in b.items() if k != "submit"}, sort_keys=True)
                              for b in self.bodies("PUT")}), 1)
        self.assertEqual({p for m, p in self.calls() if m == "PUT"}, {f"/api/v1/attempts/{attempt}"})
        self.assertEqual(self.api("prepare", "--attempt", attempt)[0], errors.EXIT_USAGE,
                         "a submitted attempt is not prepared again")

    def test_status_reads_compute_and_updates_the_record(self):
        attempt = self.prepare()
        self.api("submit", attempt)
        code, out, _ = self.api("--json", "status", attempt)
        self.assertEqual(code, 0)
        result = json.loads(out)["result"]
        self.assertEqual(result["attempt"]["scheduler"]["summary"], "PENDING")
        self.assertEqual(self.calls()[-1], ("GET", f"/api/v1/attempts/{attempt}"))
        self.assertEqual(self.ws.records()[attempt]["last_observed"]["job_id"], "920001")

    def test_status_of_an_unknown_attempt(self):
        code, out, _ = self.api("--json", "status", "2c3d4e5f-6a7b-4c8d-ae9f-0a1b2c3d4e5f")
        self.assertEqual(code, errors.EXIT_ATTEMPT_NOT_FOUND)
        self.assertEqual(self.ws.records(), {})

    def test_submit_requires_a_local_record(self):
        code, _, _ = self.api("submit", "2c3d4e5f-6a7b-4c8d-ae9f-0a1b2c3d4e5f")
        self.assertEqual(code, errors.EXIT_USAGE)
        self.assertEqual(self.calls(), [])


class InterruptionAndResume(Base):
    def test_lost_prepare_response_resumes_with_the_same_attempt(self):
        self.compute.add_fault(None)  # the plan request
        self.compute.add_fault("drop_after_processing")
        code, out, _ = self.api("--json", "prepare", self.si, "--desired-output", "energy_only")
        self.assertEqual(code, errors.EXIT_SERVICE_UNAVAILABLE)
        error = json.loads(out)["error"]
        attempt = error["attempt_id"]
        self.assertIn(f"prepare --attempt {attempt}", error["suggestion"])
        self.assertEqual(self.ws.records()[attempt]["local_state"], "prepare_unconfirmed")

        code, out, _ = self.api("--json", "prepare", "--attempt", attempt)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["result"]["attempt"]["state"], "prepared")
        self.assertEqual(list(self.ws.records()), [attempt], "no new attempt was created")
        first, second = self.bodies("PUT")
        self.assertEqual(first, second)

    def test_prepare_timeout_is_a_network_timeout_and_is_not_retried(self):
        self.compute.add_fault(None)
        self.compute.add_fault("timeout_after_processing")
        code, out, _ = self.api("--json", "prepare", self.si, "--desired-output", "energy_only", timeout="0.5")
        self.assertEqual(code, errors.EXIT_NETWORK_TIMEOUT)
        self.assertEqual(len(self.bodies("PUT")), 1, "no automatic retry")
        self.assertEqual(len(self.ws.records()), 1)

    def test_timed_out_submit_is_uncertain_and_never_creates_another_attempt(self):
        attempt = self.prepare()
        self.compute.add_fault("timeout_after_processing")
        code, out, _ = self.api("--json", "submit", attempt, timeout="0.5")
        self.assertEqual(code, errors.EXIT_SUBMISSION_UNCERTAIN)
        error = json.loads(out)["error"]
        self.assertEqual((error["kind"], error["attempt_id"]), ("submission_uncertain", attempt))
        self.assertIn("Do not create a new attempt", error["suggestion"])
        self.assertEqual(self.ws.records()[attempt]["local_state"], "submit_unconfirmed")
        self.assertEqual(len([b for b in self.bodies("PUT") if b["submit"]]), 1, "no automatic retry")

        code, out, _ = self.api("--json", "status", attempt)
        self.assertEqual(json.loads(out)["result"]["attempt"]["state"], "submitted")
        code, out, _ = self.api("--json", "submit", attempt)
        self.assertEqual(code, 0)
        self.assertEqual(self.compute.sbatch_calls, 1)
        self.assertEqual(list(self.ws.records()), [attempt])
        self.assertEqual(len(self.compute.ledger), 1)

    def test_retries_preserve_the_uuid_and_the_stored_request(self):
        self.compute.add_fault(None)  # the plan request
        self.compute.add_fault("drop_after_processing")  # the first prepare: answer lost
        self.api("prepare", self.si, "--desired-output", "energy_only")
        (attempt, record), = self.ws.records().items()
        # The structure file changes afterwards; retries must not read it again.
        (self.ws.files / "Si.POSCAR").write_text("changed later\n", encoding="utf-8")
        self.assertEqual(self.api("prepare", "--attempt", attempt)[0], 0)
        self.compute.add_fault("timeout_after_processing")  # the first submit: no answer
        self.assertEqual(self.api("submit", attempt, timeout="0.5")[0], errors.EXIT_SUBMISSION_UNCERTAIN)
        self.assertEqual(self.api("submit", attempt)[0], 0)
        expected = {**record["request"], "expected_plan_digest": record["expected_plan_digest"]}
        puts = [r for r in self.compute.requests if r["method"] == "PUT"]
        self.assertEqual({r["path"] for r in puts}, {f"/api/v1/attempts/{attempt}"})
        for request in puts:
            body = json.loads(request["body"])
            self.assertEqual({k: v for k, v in body.items() if k != "submit"}, expected)
        self.assertEqual([json.loads(r["body"])["submit"] for r in puts], [False, False, True, True])
        self.assertEqual(list(self.ws.records()), [attempt])
        self.assertEqual(self.compute.sbatch_calls, 1)

    def test_generic_or_unreadable_answers_to_submit_are_uncertain_not_failures(self):
        attempt = self.prepare()
        for answer in (
            {"api_version": "v1", "error": {"code": "attempt_failed", "message": "x"}},
            {"api_version": "v1", "error": {"code": "teapot", "message": "x"}},
        ):
            # Compute processes the submission, but the client receives only this answer.
            self.compute.raw_override.append(
                lambda status, doc, answer=answer: (500, "application/json", json.dumps(answer).encode()))
            code, out, _ = self.api("--json", "submit", attempt)
            self.assertEqual(code, errors.EXIT_SUBMISSION_UNCERTAIN)
            error = json.loads(out)["error"]
            self.assertEqual(error["kind"], "submission_uncertain")
            self.assertIn("Do not create a new attempt", error["suggestion"])
            self.assertEqual(self.ws.records()[attempt]["local_state"], "submit_unconfirmed")
        code, out, _ = self.api("--json", "status", attempt)
        self.assertEqual(json.loads(out)["result"]["attempt"]["state"], "submitted")
        self.assertEqual(self.compute.sbatch_calls, 1)
        self.assertEqual(list(self.ws.records()), [attempt])

    def test_unreadable_answers_to_a_sent_submit_are_uncertain(self):
        # Codex R1 review: HTTP 500 text/html after the submit PUT was sent made
        # ApiTransport.call() raise UnexpectedResponse, reported as exit 4 with no warning.
        attempt = self.prepare()
        record = self.ws.records()[attempt]
        stored = {**record["request"], "expected_plan_digest": record["expected_plan_digest"]}
        html = b"<html><body>Internal Server Error at /srv/bmd/app.py</body></html>"
        for answer in (
            (500, "text/html; charset=utf-8", html),
            (500, "application/json", b'{"api_version": "v1", "error": {'),
            (200, "application/json", b"{not json"),
        ):
            # Compute processes the PUT (and submits); the client receives only this answer.
            self.compute.raw_override.append(lambda status, doc, answer=answer: answer)
            code, out, err = self.api("--json", "submit", attempt)
            self.assertEqual(code, errors.EXIT_SUBMISSION_UNCERTAIN, answer[:2])
            error = json.loads(out)["error"]
            self.assertEqual((error["kind"], error["attempt_id"]), ("submission_uncertain", attempt))
            self.assertIn("may or may not have been submitted", error["message"])
            self.assertIn(f"bmd-run api status {attempt}", error["suggestion"])
            self.assertIn("Do not create a new attempt", error["suggestion"])
            for text in (out, err):
                self.assertNotIn("Internal Server Error", text)
                self.assertNotIn("/srv/bmd", text)
                self.assertNotIn("<html", text)
            after = self.ws.records()[attempt]
            self.assertEqual(after["local_state"], "submit_unconfirmed")
            self.assertEqual((after["request"], after["expected_plan_digest"], after["request_sha256"]),
                             (record["request"], record["expected_plan_digest"], record["request_sha256"]))
        submits = [json.loads(r["body"]) for r in self.compute.requests if r["method"] == "PUT" and json.loads(r["body"])["submit"]]
        self.assertEqual(len(submits), 3, "one PUT per command: no automatic retry")
        self.assertTrue(all({k: v for k, v in b.items() if k != "submit"} == stored for b in submits))
        self.assertEqual({r["path"] for r in self.compute.requests if r["method"] == "PUT"}, {f"/api/v1/attempts/{attempt}"})
        self.assertEqual(self.compute.sbatch_calls, 1)
        self.assertEqual(list(self.ws.records()), [attempt])
        code, out, _ = self.api("--json", "status", attempt)
        self.assertEqual(json.loads(out)["result"]["attempt"]["state"], "submitted")

    def test_unreadable_status_answers_keep_their_read_only_classification(self):
        attempt = self.prepare()
        self.compute.raw_override.append(lambda status, doc: (500, "text/html", b"<html>oops</html>"))
        code, _, _ = self.api("status", attempt)
        self.assertEqual(code, errors.EXIT_UNEXPECTED_RESPONSE)

    def test_compute_reported_uncertain_submission_is_never_resubmitted(self):
        attempt = self.prepare()
        self.compute.add_fault("sbatch_uncertain")
        code, out, _ = self.api("--json", "submit", attempt)
        self.assertEqual(code, errors.EXIT_SUBMISSION_UNCERTAIN)
        self.assertEqual(json.loads(out)["error"]["attempt"]["state"], "submission_uncertain")
        self.assertEqual(self.ws.records()[attempt]["local_state"], "submission_uncertain")
        code, _, _ = self.api("submit", attempt)
        self.assertEqual(code, errors.EXIT_SUBMISSION_UNCERTAIN)
        self.assertEqual(self.compute.sbatch_calls, 1)
        self.assertEqual(list(self.ws.records()), [attempt])

    def test_submit_with_the_tunnel_down_sends_nothing_and_keeps_the_state(self):
        attempt = self.prepare()
        self.server.httpd.shutdown()
        self.server.httpd.server_close()  # the tunnel's port now refuses connections
        code, out, _ = self.api("--json", "submit", attempt)
        self.assertEqual(code, errors.EXIT_SERVICE_UNAVAILABLE)
        self.assertIn(f"Nothing was sent. Retry with: bmd-run api submit {attempt}", json.loads(out)["error"]["suggestion"])
        record = self.ws.records()[attempt]
        self.assertEqual((record["local_state"], record["submit_requested"]), ("prepared", False))
        self.assertEqual(self.compute.sbatch_calls, 0)

    def test_rejected_and_busy_submissions_leave_the_attempt_prepared(self):
        attempt = self.prepare()
        for fault, exit_code in (("submit_failed", errors.EXIT_REMOTE_OPERATION_FAILED),
                                 ("remote_busy", errors.EXIT_SERVICE_UNAVAILABLE),
                                 ("active_job_cap_exceeded", errors.EXIT_QUOTA_EXCEEDED)):
            self.compute.add_fault(fault)
            code, _, _ = self.api("submit", attempt)
            self.assertEqual(code, exit_code, fault)
            self.assertEqual(self.ws.records()[attempt]["local_state"], "prepared", fault)
        self.assertEqual(self.api("submit", attempt)[0], 0)
        self.assertEqual(list(self.ws.records()), [attempt])


@unittest.skipUnless(os.name == "posix", "attempt records need POSIX permissions")
class SubmitBoundaryWithFakeTransport(unittest.TestCase):
    """machine.submit() against a stub transport whose call() raises after sending."""

    def test_unexpected_response_after_a_sent_submit_is_uncertain(self):
        import tempfile
        from pathlib import Path

        from bmd_run import machine
        from bmd_run.attempt_store import AttemptStore, new_record

        attempt = "6f1d2c3b-4a59-4e68-8b7a-9c0d1e2f3a4b"
        request = fixture("plan_si_poscar_energy_only")["request"]["body"]

        class FakeApi:
            base_url = "http://127.0.0.1:18000"

            def __init__(self):
                self.calls = []

            def call(self, endpoint, *, attempt_id=None, body=None):
                self.calls.append((endpoint.name, attempt_id, body))
                raise errors.UnexpectedResponse("The BMD Compute API response is not JSON.", http_status=500)

        with tempfile.TemporaryDirectory() as tmp:
            store = AttemptStore(Path(tmp) / "state")
            record = new_record(attempt_id=attempt, api_origin=FakeApi.base_url, request=request,
                                structure_source={"file_name": "Si.POSCAR", "sha256": "0" * 64, "format": "poscar"},
                                expected_plan_digest=SI_DIGEST)
            record["local_state"] = "prepared"
            store.create(record)
            api = FakeApi()
            with self.assertRaises(errors.SubmissionUncertain) as caught:
                machine.submit(api, store, attempt)
            self.assertEqual(caught.exception.exit_code, errors.EXIT_SUBMISSION_UNCERTAIN)
            self.assertEqual(caught.exception.details["attempt_id"], attempt)
            self.assertEqual(len(api.calls), 1, "no automatic retry")
            name, called_id, body = api.calls[0]
            self.assertEqual((name, called_id, body["submit"]), ("PUT_ATTEMPT", attempt, True))
            after = store.load(attempt)
            self.assertEqual(after["local_state"], "submit_unconfirmed")
            self.assertEqual((after["attempt_id"], after["request"], after["expected_plan_digest"]),
                             (attempt, request, SI_DIGEST))
            self.assertEqual(sorted(p.name for p in (Path(tmp) / "state" / "attempts").iterdir()), [f"{attempt}.json"])


class ChangedRequests(Base):
    def test_resume_takes_no_scientific_arguments(self):
        attempt = self.prepare()
        before = len(self.compute.requests)
        for extra in (["--cpus", "48"], [self.si], ["--desired-output", "dos"], ["--expect-plan-digest", SI_DIGEST]):
            code, _, _ = self.api("prepare", "--attempt", attempt, *extra)
            self.assertEqual(code, errors.EXIT_USAGE, extra)
        self.assertEqual(len(self.compute.requests), before)

    def test_accidental_request_modification_without_checksum_update_is_refused(self):
        # Accidental corruption or a partial edit: the request no longer matches its stored SHA-256.
        attempt = self.prepare()
        path = self.ws.state / "attempts" / f"{attempt}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["request"]["resources"] = {"cpus": 96}
        path.write_text(json.dumps(record), encoding="utf-8")
        before = len(self.compute.requests)
        for command in (["submit", attempt], ["prepare", "--attempt", attempt]):
            code, out, _ = self.api("--json", *command)
            self.assertEqual(code, errors.EXIT_LOCAL_STATE)
            self.assertEqual(json.loads(out)["error"]["kind"], "local_state_error")
        self.assertEqual(len(self.compute.requests), before)

    def test_compute_refuses_a_different_request_for_a_bound_attempt(self):
        attempt = self.prepare()
        # A deliberately edited and rehashed record passes the local consistency check (by
        # design: deliberate local modification is outside the threat model), but this attempt
        # UUID is already registered, so Compute's binding refuses the changed request and
        # nothing is prepared or submitted.
        from bmd_run.attempt_store import request_sha256

        record_path = self.ws.state / "attempts" / f"{attempt}.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["request"] = fixture("plan_si_energy_only_resources")["request"]["body"]
        record["request_sha256"] = request_sha256(record["request"])
        record_path.write_text(json.dumps(record), encoding="utf-8")
        code, out, _ = self.api("--json", "submit", attempt)
        self.assertEqual(code, errors.EXIT_ATTEMPT_CONFLICT)
        self.assertEqual(json.loads(out)["error"]["compute_code"], "attempt_request_mismatch")
        self.assertEqual(self.compute.sbatch_calls, 0)

    def test_expected_plan_digest_mismatch_records_and_prepares_nothing(self):
        code, out, _ = self.api("--json", "prepare", self.si, "--desired-output", "energy_only",
                                "--expect-plan-digest", "sha256:" + "0" * 64)
        self.assertEqual(code, errors.EXIT_PLAN_DIGEST_MISMATCH)
        self.assertEqual(json.loads(out)["error"]["resolved_plan_digest"], SI_DIGEST)
        self.assertEqual(self.calls(), [("POST", "/api/v1/plans")])
        self.assertEqual(self.ws.records(), {})

    def test_compute_plan_digest_mismatch_is_classified(self):
        attempt = self.prepare()
        path = self.ws.state / "attempts" / f"{attempt}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["expected_plan_digest"] = "sha256:" + "1" * 64
        path.write_text(json.dumps(record), encoding="utf-8")
        code, out, _ = self.api("--json", "submit", attempt)
        self.assertEqual(code, errors.EXIT_PLAN_DIGEST_MISMATCH)
        self.assertEqual(json.loads(out)["error"]["resolved_plan_digest"], SI_DIGEST)
        self.assertEqual(self.compute.sbatch_calls, 0)

    def test_records_are_bound_to_their_api_origin(self):
        attempt = self.prepare()
        before = len(self.compute.requests)
        code, _, _ = self.api("status", attempt, "--api-url", "http://127.0.0.1:1")
        self.assertEqual(code, errors.EXIT_ATTEMPT_CONFLICT)
        self.assertEqual(len(self.compute.requests), before)

    def test_unsafe_state_directory_is_refused_before_any_request(self):
        self.ws.state.mkdir(mode=0o755)
        os.chmod(self.ws.state, 0o755)
        code, out, _ = self.api("--json", "prepare", self.si, "--desired-output", "energy_only")
        self.assertEqual(code, errors.EXIT_LOCAL_STATE)
        self.assertEqual(self.calls(), [])


class MalformedResponses(Base):
    def _mutate_next(self, mutate):
        def override(status, document):
            document = mutate(json.loads(json.dumps(document)))
            return status, "application/json", json.dumps(document).encode()
        self.compute.raw_override.append(override)

    def test_malformed_plan_responses_are_refused(self):
        for mutate in (
            lambda d: {**d, "schema": "something.else"},
            lambda d: {**d, "plan_digest": "md5:abc"},
            lambda d: {k: v for k, v in d.items() if k != "resources"},
            lambda d: {**d, "request": {**d["request"], "desired_output": "electronic_dos"}},
            lambda d: {**d, "structure": {**d["structure"], "natoms": "two"}},
            lambda d: [d],
        ):
            self._mutate_next(mutate)
            code, out, _ = self.api("--json", "plan", self.si, "--desired-output", "energy_only")
            self.assertEqual(code, errors.EXIT_UNEXPECTED_RESPONSE)
            self.assertIsNone(json.loads(out)["result"])

    def test_malformed_attempt_responses_are_refused(self):
        attempt = self.prepare()
        for mutate in (
            lambda d: {**d, "attempt_id": "3d4e5f6a-7b8c-4d9e-bf0a-1b2c3d4e5f6a"},
            lambda d: {**d, "plan_digest": "sha256:" + "2" * 64},
            lambda d: {**d, "state": "running_somewhere"},
            lambda d: {**d, "submission": {**d["submission"], "job_id": "12; scancel"}},
        ):
            self._mutate_next(mutate)
            code, _, _ = self.api("status", attempt)
            self.assertEqual(code, errors.EXIT_UNEXPECTED_RESPONSE)

    def test_unusable_answers_to_prepare_and_submit_leave_the_outcome_unconfirmed(self):
        passthrough = lambda status, doc: (status, "application/json", json.dumps(doc).encode())  # noqa: E731
        unusable = lambda status, doc: (200, "application/json", b'{"schema": "x"}')  # noqa: E731
        self.compute.raw_override.extend([passthrough, unusable])  # the plan, then the prepare
        code, out, _ = self.api("--json", "prepare", self.si, "--desired-output", "energy_only")
        self.assertEqual(code, errors.EXIT_UNEXPECTED_RESPONSE)
        (attempt, record), = self.ws.records().items()
        self.assertEqual(record["local_state"], "prepare_unconfirmed")
        self.assertEqual(self.api("prepare", "--attempt", attempt)[0], 0)

        self._mutate_next(lambda d: {**d, "state": "running_somewhere"})
        code, out, _ = self.api("--json", "submit", attempt)
        self.assertEqual(code, errors.EXIT_UNEXPECTED_RESPONSE)
        self.assertEqual(self.ws.records()[attempt]["local_state"], "submit_unconfirmed")
        code, out, _ = self.api("--json", "status", attempt)
        self.assertEqual(json.loads(out)["result"]["attempt"]["state"], "submitted")
        self.assertEqual(self.compute.sbatch_calls, 1)

    def test_unknown_error_codes_are_not_relayed(self):
        self.compute.raw_override.append(lambda status, doc: (
            418, "application/json",
            json.dumps({"api_version": "v1", "error": {"code": "teapot", "message": "brew"}}).encode()))
        code, out, _ = self.api("--json", "plan", self.si, "--desired-output", "energy_only")
        self.assertEqual(code, errors.EXIT_UNEXPECTED_RESPONSE)
        self.assertNotIn("teapot", out)
        self.assertNotIn("brew", out)


class Redaction(Base):
    def test_a_compute_error_echoing_the_token_never_reaches_output(self):
        echoed = self.token

        def override(status, document):
            return 401, "application/json", json.dumps({"api_version": "v1", "error": {
                "code": "unauthenticated", "message": f"bad token {echoed}", "suggestion": echoed}}).encode()

        secret = self.token.rsplit(".", 1)[1]
        for mode in (["--json"], []):  # JSON output, then human output
            self.compute.raw_override.append(override)
            code, out, err = self.api(*mode, "plan", self.si, "--desired-output", "energy_only")
            self.assertEqual(code, errors.EXIT_AUTHENTICATION_FAILED)
            for text in (out, err):
                self.assertNotIn(self.token, text)
                self.assertNotIn(secret, text)
                self.assertNotIn("bad token", text)
            self.assertTrue(out or err)


class LegacyCompatibility(unittest.TestCase):
    def test_v010_read_build_plan_needs_no_token_and_uses_only_the_frozen_table(self):
        from _support import FIXTURES, run_cli

        code, out, _, network = run_cli(["plan", "energy", str(FIXTURES / "Si.POSCAR")])
        self.assertEqual(code, 0)
        self.assertEqual(network.calls, [("POST", "/build-workflow")])
        self.assertTrue(all(not h.get("Authorization") for h in (r["headers"] for r in network.requests)))

    def test_api_commands_refuse_the_v1_compute_url(self):
        code, _, _ = run_api(["plan", "x", "--desired-output", "energy_only", "--compute-url", "http://compute.test:8000"])
        self.assertEqual(code, errors.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
