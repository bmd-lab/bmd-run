"""A fake BMD Compute machine API v1 served over real HTTP on 127.0.0.1.

Responses are the recorded fixtures in ``tests/fixtures/compute_api_v1``
(recorded from bmd-compute e3fbb3b; see RECORDING.md). The server adds the
state a real Compute keeps: the attempt ledger (principal, request and plan
binding), the prepare -> submit transitions, a count of submissions, scopes and
the request checks Compute performs before reading a body. Faults (lost
responses, timeouts, malformed answers) are injected per request by tests.

Nothing here contacts a live VM or POWER.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import secrets
import shutil
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from _support import ROOT  # noqa: F401  (puts src/ on sys.path)

API_FIXTURES = ROOT / "tests" / "fixtures" / "compute_api_v1"
SI_POSCAR = API_FIXTURES / "Si.POSCAR"
NIO_POSCAR = API_FIXTURES / "NiO.POSCAR"
SI_CIF = API_FIXTURES / "Si.cif"


def fixture(name: str) -> dict:
    return json.loads((API_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def new_token() -> str:
    return f"bmdc1.{secrets.token_hex(8)}.{secrets.token_urlsafe(32)}"


PLAN_CASES = (
    "plan_si_poscar_energy_only",
    "plan_si_cif_energy_only",
    "plan_nio_poscar_electronic_dos",
    "plan_si_custom_relax_static",
    "plan_si_energy_only_resources",
    "plan_error_structure_invalid",
    "plan_error_calculation_invalid",
    "plan_error_unknown_desired_output",
    "plan_error_invalid_request",
)


def _custom_canonical(body: dict) -> dict:
    """Compute fills stage defaults (modifiers [], options {}); match on the filled form."""

    body = copy.deepcopy(body)
    custom = body.get("workflow", {}).get("custom")
    if custom is not None:
        for stage in custom.get("stages", []):
            stage.setdefault("modifiers", [])
            stage.setdefault("options", {})
    if body.get("structure"):
        body["structure"]["format"] = str(body["structure"].get("format", "")).lower()
    return body


class FakeCompute:
    """State and behaviour of the fake service. Thread-safe."""

    def __init__(self):
        self.lock = threading.Lock()
        self.tokens = {}
        self.plans = {}
        for name in PLAN_CASES:
            case = fixture(name)
            key = canonical(_custom_canonical(case["request"]["body"]))
            self.plans[key] = case["response"]
        self.ledger = {}
        self.requests = []
        self.sbatch_calls = 0
        self.next_job_id = 920001
        self.faults = []  # consumed in order: callables(handler_context) or named faults
        self.observers = []  # callables(method, path, body) run before processing

    # --- configuration ---------------------------------------------------------

    def issue(self, principal: str, scopes) -> str:
        token = new_token()
        with self.lock:
            self.tokens[token] = (principal, frozenset(scopes))
        return token

    def add_fault(self, fault) -> None:
        with self.lock:
            self.faults.append(fault)

    # --- request handling ---------------------------------------------------------

    def _error(self, status, code, message="Compute message that the client must never relay.", **extra):
        return status, {"api_version": "v1", "error": {"code": code, "message": message, **extra}}

    def handle(self, method, raw_path, headers, body_bytes):
        with self.lock:
            self.requests.append({"method": method, "path": raw_path, "headers": dict(headers), "body": body_bytes})
            fault = self.faults.pop(0) if self.faults else None
        for observer in list(self.observers):
            observer(method, raw_path, body_bytes)
        if fault == "refuse_before_processing":
            return "drop", None
        if "?" in raw_path:
            return self._error(400, "query_not_allowed")
        authorizations = headers.get_all("Authorization") or []
        token = None
        if len(authorizations) == 1 and authorizations[0].startswith("Bearer "):
            token = authorizations[0][len("Bearer "):]
        principal = self.tokens.get(token)
        if principal is None:
            return self._error(401, "unauthenticated", "A valid BMD Compute API bearer token is required.")
        name, scopes = principal

        if method == "POST" and raw_path == "/api/v1/plans":
            if "plan" not in scopes:
                return self._error(403, "insufficient_scope")
            body = self._json(headers, body_bytes)
            if isinstance(body, tuple):
                return body
            return self._plan(body)
        if raw_path.startswith("/api/v1/attempts/"):
            attempt_id = raw_path[len("/api/v1/attempts/"):]
            if method == "GET":
                if "read" not in scopes:
                    return self._error(403, "insufficient_scope")
                return self._get(name, attempt_id)
            if method == "PUT":
                if not scopes & {"prepare", "submit"}:
                    return self._error(403, "insufficient_scope")
                body = self._json(headers, body_bytes)
                if isinstance(body, tuple):
                    return body
                if ("submit" if body.get("submit") else "prepare") not in scopes:
                    return self._error(403, "insufficient_scope")
                result = self._put(name, attempt_id, body, fault)
                return result
        return self._error(404, "not_found")

    def _json(self, headers, body_bytes):
        if headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self._error(415, "unsupported_media_type")
        try:
            return json.loads(body_bytes.decode("utf-8"))
        except ValueError:
            return self._error(400, "invalid_json")

    def _plan(self, body):
        response = self.plans.get(canonical(_custom_canonical(body)))
        if response is None:
            return self._error(422, "structure_invalid", "unrecorded request in the fake service")
        return response["status"], copy.deepcopy(response["body"])

    def _plan_digest(self, request):
        status, body = self._plan(request)
        return body.get("plan_digest") if status == 200 else None

    def _projection(self, record):
        template = copy.deepcopy(fixture("attempt_get_submitted")["response"]["body"])
        template["attempt_id"] = record["attempt_id"]
        template["plan_digest"] = record["plan_digest"]
        template["state"] = record["state"]
        template["submission"] = {
            "requested": record["requested"],
            "job_id": record["job_id"],
            "submitted_at_local": "2026-10-10 00:00:00" if record["job_id"] else None,
        }
        template["scheduler"] = record.get("scheduler")
        return template

    def _put(self, principal, attempt_id, body, fault):
        # Compute's order: an existing binding (principal, request) is checked first, then the plan digest.
        request = {key: body[key] for key in ("structure", "workflow", "resources") if key in body}
        request_sha = hashlib.sha256(canonical(request).encode()).hexdigest()
        with self.lock:
            existing = self.ledger.get(attempt_id)
        if existing is not None:
            if existing["principal"] != principal:
                return self._error(403, "attempt_forbidden")
            if existing["request_sha"] != request_sha:
                return self._error(409, "attempt_request_mismatch")
        digest = self._plan_digest(request)
        if digest is None:
            return self._plan(request)
        if body.get("expected_plan_digest") != digest:
            return self._error(409, "plan_digest_mismatch", plan_digest=digest)
        with self.lock:
            record = self.ledger.get(attempt_id)
            if record is None:
                record = {"attempt_id": attempt_id, "principal": principal, "request_sha": request_sha,
                          "plan_digest": digest, "state": "registered", "requested": False, "job_id": None,
                          "scheduler": None}
                self.ledger[attempt_id] = record
            if fault == "prepare_failed" and record["state"] == "registered":
                return self._error(502, "prepare_failed")
            if record["state"] == "registered":
                record["state"] = "prepared"
            if body.get("submit"):
                if record["state"] == "submission_uncertain":
                    return self._error(409, "submission_uncertain", attempt=self._projection(record))
                if record["state"] == "prepared":
                    record["requested"] = True
                    if fault in ("active_job_cap_exceeded", "submission_cap_exceeded"):
                        record["requested"] = False
                        return self._error(429, fault)
                    if fault == "submit_failed":
                        self.sbatch_calls += 1
                        return self._error(502, "submit_failed")
                    if fault == "remote_busy":
                        return self._error(503, "remote_busy")
                    self.sbatch_calls += 1
                    if fault == "sbatch_uncertain":
                        record["state"] = "submission_uncertain"
                        return self._error(409, "submission_uncertain", attempt=self._projection(record))
                    record["state"] = "submitted"
                    record["job_id"] = str(self.next_job_id)
                    self.next_job_id += 1
            projection = self._projection(record)
        if fault in ("drop_after_processing", "timeout_after_processing"):
            return fault, None
        return 200, projection

    def _get(self, principal, attempt_id):
        with self.lock:
            record = self.ledger.get(attempt_id)
            if record is None:
                return self._error(404, "attempt_not_found")
            if record["principal"] != principal:
                return self._error(403, "attempt_forbidden")
            if record["job_id"]:
                record["scheduler"] = copy.deepcopy(fixture("attempt_get_submitted")["response"]["body"]["scheduler"])
            return 200, self._projection(record)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fake-bmd-compute"
    sys_version = ""

    def log_message(self, *args):  # silence
        pass

    def _serve(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        compute = self.server.compute
        override = compute.raw_override.pop(0) if compute.raw_override else None
        status, document = compute.handle(self.command, self.path, self.headers, body)
        if status == "drop":
            self.close_connection = True
            return
        if status == "drop_after_processing":
            self.close_connection = True  # processed, but no response is written
            return
        if status == "timeout_after_processing":
            time.sleep(compute.slow_seconds)
            self.close_connection = True
            return
        if override is not None:
            status, content_type, data = override(status, document)
        else:
            content_type = "application/json"
            data = json.dumps(document).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        if status in (301, 302, 307):
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = _serve


class FakeComputeServer:
    def __init__(self):
        self.compute = FakeCompute()
        self.compute.raw_override = []
        self.compute.slow_seconds = 1.5
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.compute = self.compute
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class Workspace:
    """A temporary home: protected token files, a state directory, structure copies."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bmd-run-api-")
        self.root = Path(self._tmp.name)
        self.state = self.root / "state"
        self.files = self.root / "files"
        self.files.mkdir()
        for source in (SI_POSCAR, NIO_POSCAR, SI_CIF):
            shutil.copy2(source, self.files / source.name)

    def token_file(self, token: str, name: str = "token", mode: int = 0o600) -> Path:
        path = self.root / name
        path.write_text(token + "\n", encoding="ascii")
        os.chmod(path, mode)
        return path

    def records(self):
        directory = self.state / "attempts"
        if not directory.is_dir():
            return {}
        return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in directory.glob("*.json")}

    def all_state_text(self) -> str:
        if not self.state.exists():
            return ""
        return "".join(p.read_text(encoding="utf-8") for p in self.state.rglob("*") if p.is_file())

    def close(self):
        self._tmp.cleanup()


def run_api(argv, *, server=None, workspace=None, token_file=None, env=None, timeout="5"):
    """Run ``bmd-run api ...`` in-process against the fake server; returns (code, stdout, stderr)."""

    from bmd_run import cli

    environ = {"HOME": str(workspace.root) if workspace else "/nonexistent"}
    if workspace is not None:
        environ["BMD_RUN_STATE_DIR"] = str(workspace.state)
    if server is not None:
        environ["BMD_RUN_API_URL"] = server.url
    environ.update(env or {})
    full = ["api"] + list(argv)
    if token_file is not None and "--token-file" not in full:
        full += ["--token-file", str(token_file)]
    if timeout is not None and "--timeout" not in full:
        full += ["--timeout", timeout]
    stdout, stderr = io.StringIO(), io.StringIO()
    code = cli.main(full, environ=environ, stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


@contextmanager
def fake_compute():
    with FakeComputeServer() as server:
        yield server
