"""Record BMD Compute machine-API v1 responses for bmd-run's offline tests.

Run this from a bmd-compute checkout at the reference commit, inside that
checkout's own test environment (it imports Compute's application and the
in-memory fake POWER from Compute's test suite)::

    python tools/record_machine_api_fixtures.py record  --compute-checkout PATH
    python tools/record_machine_api_fixtures.py compare --compute-checkout PATH

Nothing here opens a network connection. Requests go to Compute's FastAPI
application in-process (Starlette ``TestClient``), every remote operation goes
to Compute's own in-memory fake POWER (no SSH, no scheduler) and tokens are
generated for the run, written only to a temporary token store and never
recorded.

Normalisations (documented in tests/fixtures/compute_api_v1/RECORDING.md):

* M1 UTC timestamps (``created_at``, ``checked_at``) -> ``2026-10-10T00:00:00Z``;
* M2 local submission time (``submitted_at_local``) -> ``2026-10-10 00:00:00``.

Everything else is byte-for-byte Compute's JSON (re-serialised with sorted
keys and two-space indentation).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "compute_api_v1"
V1_FIXTURES = ROOT / "tests" / "fixtures" / "compute_v1.0.0"
REFERENCE_COMMIT = "e3fbb3beaf5c0084023df9fcdd76deea4edc4778"
LOOPBACK = ("127.0.0.1", 50000)

ATTEMPT_MAIN = "6f1d2c3b-4a59-4e68-8b7a-9c0d1e2f3a4b"
ATTEMPT_DIGEST_MISMATCH = "0a1b2c3d-4e5f-4a6b-8c7d-8e9f0a1b2c3d"
ATTEMPT_SCOPE = "1b2c3d4e-5f6a-4b7c-9d8e-9f0a1b2c3d4e"
ATTEMPT_UNKNOWN = "2c3d4e5f-6a7b-4c8d-ae9f-0a1b2c3d4e5f"
ATTEMPT_UNCERTAIN = "3d4e5f6a-7b8c-4d9e-bf0a-1b2c3d4e5f6a"
ATTEMPT_SUBMIT_FAILED = "4e5f6a7b-8c9d-4e0f-8a1b-2c3d4e5f6a7b"
ATTEMPT_CAPPED = "5f6a7b8c-9d0e-4f1a-9b2c-3d4e5f6a7b8c"
ATTEMPT_LABELLED = "15152671-4724-4962-8d4c-78425ed6b968"

_UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_LOCAL = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$")


def _normalise(value, key=None):
    if isinstance(value, dict):
        return {k: _normalise(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    if isinstance(value, str):
        if key in ("created_at", "checked_at") and _UTC.match(value):
            return "2026-10-10T00:00:00Z"  # M1
        if key == "submitted_at_local" and _LOCAL.match(value):
            return "2026-10-10 00:00:00"  # M2
    return value


def _setup(compute: Path, workdir: Path):
    sys.path[:0] = [str(compute / "tests"), str(compute)]
    os.chdir(compute)
    import backend.paramiko_remote as paramiko_remote  # noqa: E402
    import main  # noqa: E402
    from backend.remote import RemoteCommandResult  # noqa: E402
    from compute_api import auth as api_auth  # noqa: E402
    from compute_api import execution  # noqa: E402
    from compute_api import ledger as ledger_module  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402
    from test_machine_api_execution import FakePower  # noqa: E402

    power = FakePower()
    execution.RUNNER_FACTORY = power.factory
    paramiko_remote.SUBMISSION_ATTEMPT_STATE_WAIT_S = 0.05
    paramiko_remote.SUBMISSION_ATTEMPT_STATE_POLL_S = 0.01

    state = workdir / "api_state"
    state.mkdir(mode=0o700)
    os.environ[ledger_module.STATE_DIR_ENV] = str(state)

    scopes = {
        "runner": ["plan", "read", "prepare", "submit"],
        "preparer": ["plan", "read", "prepare"],
        "planner": ["plan", "read"],
        "submit_only": ["submit"],
        # Another client of the same Compute API that sets labels (bmd-run itself sends none).
        "labelling_client": ["plan", "read", "prepare", "submit"],
    }
    entries, tokens = [], {}
    for principal, granted in scopes.items():
        token, token_id, verifier = api_auth.generate_token()
        tokens[principal] = token
        entries.append({"principal": principal, "token_id": token_id, "verifier": verifier,
                        "scopes": granted, "enabled": True})
    store = workdir / "api_tokens.json"
    store.write_text(json.dumps({"schema": "bmd_compute.api_tokens", "schema_version": 1,
                                 "principals": entries}), encoding="utf-8")
    os.chmod(store, 0o600)
    os.environ[api_auth.TOKENS_FILE_ENV] = str(store)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = TestClient(main.app, client=LOOPBACK)
    return client, tokens, power, RemoteCommandResult, ledger_module


def _structures():
    from pymatgen.core import Structure
    from pymatgen.io.cif import CifWriter

    si = (V1_FIXTURES / "Si.POSCAR").read_text(encoding="utf-8")
    nio = (V1_FIXTURES / "NiO.POSCAR").read_text(encoding="utf-8")
    cif = str(CifWriter(Structure.from_str(si, fmt="poscar")))
    return si, nio, cif


def record_all(compute: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="bmd-run-api-fixtures-") as tmp:
        cwd = os.getcwd()
        try:
            client, tokens, power, RemoteCommandResult, ledger_module = _setup(compute, Path(tmp))
            return _cases(client, tokens, power, RemoteCommandResult, ledger_module)
        finally:
            os.chdir(cwd)


def _cases(client, tokens, power, RemoteCommandResult, ledger_module) -> dict:
    si, nio, cif = _structures()
    out = {"structures": {"Si.POSCAR": si, "NiO.POSCAR": nio, "Si.cif": cif}, "cases": {}, "tokens": tokens}

    def call(name, method, path, token, body=None, principal=None):
        headers = {"Authorization": f"Bearer {token}"}
        if method == "GET":
            response = client.get(path, headers=headers)
        elif method == "POST":
            response = client.post(path, headers=headers, json=body)
        else:
            response = client.put(path, headers=headers, json=body)
        out["cases"][name] = {
            "case": name,
            "request": {"method": method, "path": path, "principal": principal, "body": body},
            "response": {
                "status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "body": _normalise(response.json()),
            },
        }
        return response

    def plan(structure, fmt="poscar", desired_output=None, custom=None, resources=None):
        workflow = {"custom": custom} if custom is not None else {"desired_output": desired_output}
        body = {"structure": {"format": fmt, "text": structure}, "workflow": workflow}
        if resources is not None:
            body["resources"] = resources
        return body

    def stage(stage_type, theory, modifiers=()):
        return {"stage_type": stage_type, "theory": theory, "modifiers": list(modifiers), "options": {}}

    p = "/api/v1/plans"
    si_energy = plan(si, desired_output="energy_only")
    call("plan_si_poscar_energy_only", "POST", p, tokens["planner"], si_energy, "planner")
    call("plan_si_cif_energy_only", "POST", p, tokens["planner"], plan(cif, fmt="cif", desired_output="energy_only"), "planner")
    call("plan_nio_poscar_electronic_dos", "POST", p, tokens["planner"], plan(nio, desired_output="electronic_dos"), "planner")
    call("plan_si_custom_relax_static", "POST", p, tokens["planner"],
         plan(si, custom={"stages": [stage("relax", "pbe"), stage("static", "pbe")]}), "planner")
    call("plan_si_energy_only_resources", "POST", p, tokens["planner"],
         plan(si, desired_output="energy_only", resources={"cpus": 48, "memory_gb": 64, "walltime": "12:00:00"}), "planner")
    call("plan_error_unauthenticated", "POST", p, "bmdc1.0000000000000000." + "A" * 43, si_energy, None)
    call("plan_error_insufficient_scope", "POST", p, tokens["submit_only"], si_energy, "submit_only")
    call("plan_error_structure_invalid", "POST", p, tokens["planner"], plan("not a structure\n", desired_output="energy_only"), "planner")
    call("plan_error_calculation_invalid", "POST", p, tokens["planner"],
         plan(si, custom={"stages": [stage("static", "hse06", ["soc"])]}), "planner")
    call("plan_error_unknown_desired_output", "POST", p, tokens["planner"], plan(si, desired_output="nonsense_output"), "planner")
    bad_resources = plan(si, desired_output="energy_only")
    bad_resources["resources"] = {"cpus": 48, "nodes": 2}
    call("plan_error_invalid_request", "POST", p, tokens["planner"], bad_resources, "planner")

    digest = out["cases"]["plan_si_poscar_energy_only"]["response"]["body"]["plan_digest"]

    def attempt(submit, base=si_energy, expected=digest):
        body = json.loads(json.dumps(base))
        body["expected_plan_digest"] = expected
        body["submit"] = submit
        return body

    a = f"/api/v1/attempts/{ATTEMPT_MAIN}"
    # bmd-run sends no labels; the bodies below are exactly what it sends.
    call("attempt_prepare", "PUT", a, tokens["runner"], attempt(False), "runner")
    call("attempt_prepare_repeat", "PUT", a, tokens["runner"], attempt(False), "runner")
    call("attempt_get_prepared", "GET", a, tokens["runner"], None, "runner")
    call("attempt_submit", "PUT", a, tokens["runner"], attempt(True), "runner")
    call("attempt_submit_repeat", "PUT", a, tokens["runner"], attempt(True), "runner")
    call("attempt_get_submitted", "GET", a, tokens["runner"], None, "runner")
    changed = attempt(True)
    changed["resources"] = {"cpus": 48}
    call("attempt_error_request_mismatch", "PUT", a, tokens["runner"], changed, "runner")
    call("attempt_error_get_unauthenticated", "GET", a, "bmdc1.0000000000000000." + "A" * 43, None, None)

    call("attempt_error_plan_digest_mismatch", "PUT", f"/api/v1/attempts/{ATTEMPT_DIGEST_MISMATCH}", tokens["runner"],
         attempt(False, expected="sha256:" + "0" * 64), "runner")
    call("attempt_error_insufficient_scope", "PUT", f"/api/v1/attempts/{ATTEMPT_SCOPE}", tokens["preparer"],
         attempt(True), "preparer")
    call("attempt_error_not_found", "GET", f"/api/v1/attempts/{ATTEMPT_UNKNOWN}", tokens["runner"], None, "runner")

    power.submit_error_result = RemoteCommandResult(command="sbatch", returncode=1, stderr="sbatch: error: invalid account")
    call("attempt_error_submit_failed", "PUT", f"/api/v1/attempts/{ATTEMPT_SUBMIT_FAILED}", tokens["runner"],
         attempt(True), "runner")
    power.submit_error_result = None

    power.submit_exception = TimeoutError("connection lost after sbatch")
    u = f"/api/v1/attempts/{ATTEMPT_UNCERTAIN}"
    call("attempt_error_submission_uncertain", "PUT", u, tokens["runner"], attempt(True), "runner")
    power.submit_exception = None
    call("attempt_get_uncertain", "GET", u, tokens["runner"], None, "runner")

    # ATTEMPT_MAIN (submitted) and ATTEMPT_UNCERTAIN (uncertain) are both active.
    call("attempt_error_active_job_cap", "PUT", f"/api/v1/attempts/{ATTEMPT_CAPPED}", tokens["runner"],
         attempt(True), "runner")

    saved = os.environ.pop(ledger_module.STATE_DIR_ENV)
    call("attempt_error_execution_not_configured", "PUT", f"/api/v1/attempts/{ATTEMPT_CAPPED}", tokens["runner"],
         attempt(False), "runner")
    os.environ[ledger_module.STATE_DIR_ENV] = saved

    # A completed, labelled attempt created by another client (live POWER acceptance shape):
    # labels are a documented request field, and bmd-run status must read such attempts.
    labelled = attempt(False)
    labelled["labels"] = {"campaign": "phase1b-acceptance", "cell": "si-pbe-static-prepare"}
    path = f"/api/v1/attempts/{ATTEMPT_LABELLED}"
    call("attempt_labelled_prepare", "PUT", path, tokens["labelling_client"], labelled, "labelling_client")
    labelled["submit"] = True
    submitted = call("attempt_labelled_submit", "PUT", path, tokens["labelling_client"], labelled, "labelling_client")
    power.job_states[submitted.json()["submission"]["job_id"]] = "COMPLETED"
    call("attempt_get_labelled_completed", "GET", path, tokens["labelling_client"], None, "labelling_client")
    return out


def _render(document) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _files(recorded: dict) -> dict:
    files = {name: text for name, text in recorded["structures"].items()}
    for name, case in recorded["cases"].items():
        files[f"{name}.json"] = _render(case)
    for name, text in files.items():
        for token in recorded["tokens"].values():
            secret = token.rsplit(".", 1)[-1]
            if token in text or secret in text:
                raise SystemExit(f"refusing to write {name}: it contains token material")
    manifest = {
        "schema": "bmd_run.compute_api_fixtures",
        "schema_version": 1,
        "compute_commit": REFERENCE_COMMIT,
        "files": {name: hashlib.sha256(text.encode("utf-8")).hexdigest() for name, text in sorted(files.items())},
    }
    files["MANIFEST.json"] = _render(manifest)
    return files


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("mode", choices=("record", "compare"))
    parser.add_argument("--compute-checkout", required=True, type=Path)
    args = parser.parse_args(argv)
    compute = args.compute_checkout.resolve()
    head = subprocess.run(["git", "-C", str(compute), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if head != REFERENCE_COMMIT:
        print(f"warning: Compute checkout is at {head or 'unknown'}, reference is {REFERENCE_COMMIT}", file=sys.stderr)
    files = _files(record_all(compute))
    if args.mode == "record":
        FIXTURES.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (FIXTURES / name).write_text(text, encoding="utf-8")
        print(f"recorded {len(files)} files into {FIXTURES}")
        return 0
    differences = [name for name, text in files.items()
                   if not (FIXTURES / name).is_file() or (FIXTURES / name).read_text(encoding="utf-8") != text]
    extra = sorted(p.name for p in FIXTURES.iterdir() if p.is_file() and p.name not in files and p.name != "RECORDING.md")
    for name in differences:
        print(f"DIFFERS: {name}")
    for name in extra:
        print(f"UNEXPECTED: {name}")
    print("compare ok" if not differences and not extra else "compare FAILED")
    return 0 if not differences and not extra else 1


if __name__ == "__main__":
    raise SystemExit(main())
