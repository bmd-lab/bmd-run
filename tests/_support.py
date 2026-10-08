"""Shared test helpers: fixture-backed fake HTTP connection and a small JSON Schema checker.

Tests use only the Python standard library (run with ``python -m unittest``).
"""

from __future__ import annotations

import io
import json
import re
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PACKAGE = SRC / "bmd_run"
FIXTURES = ROOT / "tests" / "fixtures" / "compute_v1.0.0"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bmd_run import probes  # noqa: E402

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
TOKEN_RE = re.compile(r'name="submission_identity_token" value="([^"]+)"')


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_tokens_and_uuids() -> set:
    """Every identity token and UUID present anywhere in the recorded fixtures."""

    secrets = set()
    for path in FIXTURES.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        secrets.update(TOKEN_RE.findall(text))
        secrets.update(UUID_RE.findall(text))
    return secrets


SI = fixture_text("Si.POSCAR")
NIO = fixture_text("NiO.POSCAR")
assert SI == probes.PROBE_STRUCTURE, "Si fixture must be the identity probe structure"


def v1_router(method: str, path: str, form: dict):
    """Answer like the recorded BMD Compute v1.0.0 service. Returns (status, body)."""

    if method == "GET" and path == "/openapi.json":
        return 200, fixture_text("openapi.json")
    structure = form.get("structure")
    name = {SI: "si", NIO: "nio"}.get(structure)
    if method == "POST" and path == "/analyze":
        if name is None:
            return 400, fixture_text("analyze_bad.html")
        return 200, fixture_text(f"analyze_{name}.html")
    if method == "POST" and path == "/build-workflow":
        workflow = form.get("workflow")
        if form.get("cpus") == "7":
            return 400, fixture_text("build_si_badcpus.html")
        candidate = FIXTURES / f"build_{name}_{workflow}.html"
        if name is not None and candidate.is_file():
            return 200, candidate.read_text(encoding="utf-8")
        if workflow not in ("energy_only", "relaxed_structure", "electronic_dos", "electronic_band_structure"):
            return 400, fixture_text("build_si_unknown.html")
        raise AssertionError(f"no fixture for {name} {workflow}")
    raise AssertionError(f"unexpected request {method} {path}")


class FakeResponse:
    def __init__(self, status: int, body: str, headers=None):
        self.status = status
        self._body = io.BytesIO(body.encode("utf-8"))
        self._headers = headers or {"Content-Type": "text/html; charset=utf-8"}

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def read(self, amount=None):
        return self._body.read(amount)


class RecordingNetwork:
    """A connection factory for ``Transport`` that records every request."""

    def __init__(self, router=v1_router):
        self.router = router
        self.requests = []
        self.origins = []

    def __call__(self, origin, timeout):
        self.origins.append(origin)
        network = self

        class _Connection:
            def request(self, method, path, body=None, headers=None):
                form = dict(urllib.parse.parse_qsl(body or "", keep_blank_values=True))
                network.requests.append(
                    {"method": method, "path": path, "form": form, "headers": dict(headers or {})}
                )
                self._answer = network.router(method, path, form)

            def getresponse(self):
                status, body = self._answer[:2]
                headers = self._answer[2] if len(self._answer) > 2 else None
                return FakeResponse(status, body, headers)

            def close(self):
                pass

        return _Connection()

    @property
    def calls(self):
        return [(item["method"], item["path"]) for item in self.requests]


def transport_factory_for(network):
    from bmd_run.transport import Transport

    def factory(base_url, timeout):
        return Transport(base_url, timeout=timeout, connection_factory=network)

    return factory


def run_cli(argv, network=None, env_url="http://compute.test:8000"):
    from bmd_run import cli

    network = network or RecordingNetwork()
    stdout, stderr = io.StringIO(), io.StringIO()
    full = list(argv)
    if env_url and "--compute-url" not in full:
        full = ["--compute-url", env_url] + full
    code = cli.main(full, transport_factory=transport_factory_for(network), stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue(), network


# --- minimal JSON Schema (2020-12 subset) checker ---------------------------------

SCHEMA_PATH = PACKAGE / "schemas" / "output-v3.schema.json"


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def schema_errors(instance, schema, root=None, path="$"):
    root = root if root is not None else schema
    errors = []
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            target = target[part]
        errors += schema_errors(instance, target, root, path)
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_TYPES[t](instance) for t in types):
            return errors + [f"{path}: expected {types}, got {type(instance).__name__}"]
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in enum")
    if "pattern" in schema and isinstance(instance, str) and not re.search(schema["pattern"], instance):
        errors.append(f"{path}: does not match {schema['pattern']}")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing {key}")
        properties = schema.get("properties", {})
        for key, value in instance.items():
            if key in properties:
                errors += schema_errors(value, properties[key], root, f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property {key}")
            elif isinstance(schema.get("additionalProperties"), dict):
                errors += schema_errors(value, schema["additionalProperties"], root, f"{path}.{key}")
    if isinstance(instance, list) and "items" in schema:
        for index, item in enumerate(instance):
            errors += schema_errors(item, schema["items"], root, f"{path}[{index}]")
    if "oneOf" in schema:
        matches = [s for s in schema["oneOf"] if not schema_errors(instance, s, root, path)]
        if len(matches) != 1:
            errors.append(f"{path}: matched {len(matches)} oneOf branches")
    for sub in schema.get("allOf", []):
        errors += schema_errors(instance, sub, root, path)
    if "if" in schema and not schema_errors(instance, schema["if"], root, path):
        errors += schema_errors(instance, schema.get("then", {}), root, path)
    return errors
