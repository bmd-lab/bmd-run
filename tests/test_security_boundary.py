"""Static checks that the client package carries no authority beyond its two closed endpoint tables.

The package has two capability zones:

* the frozen v1 read/build adapter (unchanged from v0.1.0): three browser
  routes, no credentials, no file writes;
* the authenticated machine-API capability (R1): three ``/api/v1`` routes,
  a bearer token read from a protected file or one environment variable, and
  one module (``attempt_store.py``) that may create and replace private files
  under the attempt state directory.

Both zones share the absolute exclusions: no subprocesses, SSH, SFTP, SLURM,
Git, databases, dynamic code execution or deletion.

These tests inspect the source and packaging of ``bmd_run``. They prove what
the *client* can do. They cannot prove that a caller is unable to bypass the client;
that is the job of BMD Compute's own access control and of the caller's sandbox.
"""

from __future__ import annotations

import ast
import re
import unittest

from _support import PACKAGE, ROOT

from bmd_run import api_endpoints, endpoints

ALLOWED_MODULES = {
    "__future__",
    "argparse",
    "dataclasses",
    "datetime",
    "enum",
    "hashlib",
    "html.parser",
    "http.client",
    "json",
    "math",
    "os",
    "pathlib",
    "re",
    "ssl",
    "stat",
    "sys",
    "types",
    "typing",
    "urllib.parse",
    "uuid",
}
# Modules that grant network access may only be imported by the two transports.
NETWORK_MODULES = {"http.client", "ssl"}
NETWORK_MODULE_OWNERS = {"transport.py", "api_transport.py"}

READ_BUILD_MODULES = {
    "bounded.py", "endpoints.py", "incar_tags.py", "operations.py", "output.py", "probes.py",
    "transport.py", "v1_html.py", "v1_inputs.py", "v1_vocabulary.py",
}
MACHINE_MODULES = {
    "api_endpoints.py", "api_transport.py", "api_vocabulary.py", "attempt_store.py",
    "credentials.py", "machine.py", "machine_output.py",
}

FORBIDDEN_BUILTINS = {"eval", "exec", "compile", "__import__", "open", "breakpoint"}
FORBIDDEN_METHODS = {
    "write_text", "write_bytes", "mkdir", "makedirs", "unlink", "rmdir", "rename",
    "touch", "chmod", "chown", "symlink_to", "open", "system", "popen", "spawnv",
    "execv", "execve", "fork", "kill", "remove", "removedirs", "rmtree",
}
# ``os`` attributes each module may use. Everything not listed is refused. Only
# the token loader may open a file (read-only); only the attempt store may
# create, write or replace one; nothing may delete, chmod, chown or spawn.
_READ_ONLY_FILE = {"name", "lstat", "fstat", "open", "read", "close", "getuid", "O_RDONLY", "O_NOFOLLOW", "O_CLOEXEC"}
OS_ATTRIBUTES = {
    "credentials.py": {"environ"} | _READ_ONLY_FILE,
    "attempt_store.py": {"environ"} | _READ_ONLY_FILE | {
        "write", "fsync", "replace", "mkdir", "getpid", "O_WRONLY", "O_CREAT", "O_EXCL",
    },
}
DEFAULT_OS_ATTRIBUTES = {"environ"}
FILE_WRITING_OS_ATTRIBUTES = {"write", "fsync", "replace", "mkdir", "O_WRONLY", "O_CREAT", "O_EXCL"}

# Route names and submission identity fields of Compute v1 that this client must never
# reference, plus words that would indicate cluster, repository or database access.
FORBIDDEN_TEXT = [
    "/prepare-remote",
    "/submit",
    "/monitor",
    "/resume",
    "prepare-remote",
    "submission_identity_token",
    "submission_attempt_id",
    "remote_prepared",
    "monitor_state_json",
    "job_id",
    "paramiko",
    "sftp",
    "sbatch",
    "scontrol",
    "squeue",
    "scancel",
    "pymongo",
    "mongodb://",
    "subprocess",
    "git ",
]
# Text that only the machine-API modules that carry attempt state may contain.
FORBIDDEN_TEXT_EXEMPTIONS = {"job_id": {"machine.py", "machine_output.py", "attempt_store.py"}}

EXPECTED_ENDPOINTS = (
    ("openapi", "GET", "/openapi.json"),
    ("analyze", "POST", "/analyze"),
    ("build_workflow", "POST", "/build-workflow"),
)
EXPECTED_API_ENDPOINTS = (
    ("plans", "POST", "/api/v1/plans", "plan"),
    ("put_attempt", "PUT", "/api/v1/attempts/{attempt_id}", "prepare_or_submit"),
    ("get_attempt", "GET", "/api/v1/attempts/{attempt_id}", "read"),
)
# Route-like literals and the only modules allowed to contain them.
ROUTE_LITERALS = {
    "/openapi.json": {"endpoints.py", "transport.py"},
    "/analyze": {"endpoints.py", "transport.py"},
    "/build-workflow": {"endpoints.py", "transport.py"},
    "/api/v1/plans": {"api_endpoints.py", "api_transport.py"},
    "/api/v1/attempts/": {"api_endpoints.py"},
}


def _sources():
    return sorted(PACKAGE.rglob("*.py"))


def _imported_modules(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            yield node.module


class ClosedEndpointTable(unittest.TestCase):
    def test_endpoint_table_is_exactly_three_entries(self):
        self.assertEqual(endpoints.describe(), EXPECTED_ENDPOINTS)
        self.assertEqual([m.name for m in endpoints.Endpoint], ["OPENAPI", "ANALYZE", "BUILD_WORKFLOW"])

    def test_endpoint_form_fields_are_ordinary_user_fields(self):
        resolve, E = endpoints.resolve, endpoints.Endpoint
        self.assertEqual(resolve(E.OPENAPI).allowed_fields, frozenset())
        self.assertEqual(resolve(E.ANALYZE).allowed_fields, frozenset({"structure", "fmt"}))
        self.assertEqual(
            resolve(E.BUILD_WORKFLOW).allowed_fields,
            frozenset({"structure", "fmt", "workflow", "cpus", "memory_gb", "walltime", "queue"}),
        )

    def test_no_route_like_string_outside_the_tables(self):
        route = re.compile(r"^/[a-z][a-z0-9._/-]*$")
        for path in _sources():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if route.match(node.value):
                        self.assertIn(node.value, ROUTE_LITERALS, f"{path.name}: route-like string {node.value!r}")
                        self.assertIn(path.name, ROUTE_LITERALS[node.value], f"{path.name}: route {node.value!r}")


class ClosedMachineApiTable(unittest.TestCase):
    def test_machine_api_table_is_exactly_three_entries(self):
        self.assertEqual(api_endpoints.describe(), EXPECTED_API_ENDPOINTS)
        self.assertEqual([m.name for m in api_endpoints.ApiEndpoint], ["PLANS", "PUT_ATTEMPT", "GET_ATTEMPT"])

    def test_the_two_tables_are_disjoint_and_the_v1_table_is_unchanged(self):
        v1_paths = {path for _, _, path in endpoints.describe()}
        api_paths = {path for _, _, path, _ in api_endpoints.describe()}
        self.assertFalse(v1_paths & api_paths)
        self.assertTrue(all(path.startswith("/api/v1/") for path in api_paths))

    def test_bearer_credentials_are_handled_only_by_the_token_loader_and_api_transport(self):
        for path in _sources():
            text = path.read_text(encoding="utf-8")
            if "Authorization" in text:
                self.assertEqual(path.name, "api_transport.py", f"{path.name} mentions Authorization")
            if "Bearer " in text:
                self.assertEqual(path.name, "credentials.py", f"{path.name} builds a bearer header")
            if path.name in READ_BUILD_MODULES:
                for module in ("credentials", "api_transport", "attempt_store", "machine"):
                    self.assertNotIn(f"from .{module} import", text, f"{path.name} imports {module}")

    def test_machine_modules_do_not_use_the_frozen_v1_adapter(self):
        for path in _sources():
            if path.name in MACHINE_MODULES:
                text = path.read_text(encoding="utf-8")
                for module in ("v1_html", "operations", "probes", "endpoints import", "transport import Transport"):
                    self.assertNotIn(f"from .{module}", text, f"{path.name} uses {module}")

    def test_the_attempt_store_never_sees_credentials(self):
        for name in ("attempt_store.py", "machine.py", "machine_output.py"):
            text = (PACKAGE / name).read_text(encoding="utf-8")
            self.assertNotIn("from .credentials", text, name)
            self.assertNotIn("ApiToken", text, name)

    def test_attempt_ids_are_generated_in_one_place_only(self):
        hits = []
        for path in _sources():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Attribute) and node.attr in ("uuid1", "uuid3", "uuid4", "uuid5"):
                    hits.append((path.name, node.attr))
        self.assertEqual(hits, [("machine.py", "uuid4")])

    def test_only_the_submit_operation_sets_submit_true(self):
        hits = []
        for path in _sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for function in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
                for node in ast.walk(function):
                    if isinstance(node, ast.keyword) and node.arg == "submit" and isinstance(node.value, ast.Constant):
                        hits.append((path.name, function.name, node.value.value))
        self.assertEqual(
            sorted(hits),
            [("machine.py", "_send_prepare", False), ("machine.py", "submit", True)],
        )


class ImportsAndCapabilities(unittest.TestCase):
    def test_only_allowlisted_standard_library_modules_are_imported(self):
        for path in _sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for module in _imported_modules(tree):
                self.assertIn(module, ALLOWED_MODULES, f"{path.name} imports {module}")

    def test_network_modules_are_confined_to_the_transports(self):
        for path in _sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            used = set(_imported_modules(tree)) & NETWORK_MODULES
            if path.name not in NETWORK_MODULE_OWNERS:
                self.assertFalse(used, f"{path.name} imports network module(s) {used}")

    def test_no_dynamic_execution_process_or_file_writing_calls(self):
        for path in _sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            allowed_os = OS_ATTRIBUTES.get(path.name, DEFAULT_OS_ATTRIBUTES)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name):
                        self.assertNotIn(node.func.id, FORBIDDEN_BUILTINS, f"{path.name}: {node.func.id}()")
                    if isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN_METHODS:
                        # Only os-level calls a module is explicitly granted (see OS_ATTRIBUTES).
                        is_os = isinstance(node.func.value, ast.Name) and node.func.value.id == "os"
                        self.assertTrue(is_os and node.func.attr in allowed_os, f"{path.name}: .{node.func.attr}()")

    def test_os_attributes_are_limited_per_module(self):
        for path in _sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            allowed = OS_ATTRIBUTES.get(path.name, DEFAULT_OS_ATTRIBUTES)
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
                    self.assertIn(node.attr, allowed, f"{path.name}: os.{node.attr}")

    def test_only_the_attempt_store_can_write_files(self):
        writers = {name for name, allowed in OS_ATTRIBUTES.items() if allowed & FILE_WRITING_OS_ATTRIBUTES}
        self.assertEqual(writers, {"attempt_store.py"})
        self.assertFalse(any(name in OS_ATTRIBUTES["attempt_store.py"] for name in ("remove", "unlink", "rmdir", "chmod", "chown", "system")))

    def test_forbidden_routes_identity_fields_and_backends_are_not_referenced(self):
        for path in _sources():
            text = path.read_text(encoding="utf-8").lower()
            for needle in FORBIDDEN_TEXT:
                if path.name in FORBIDDEN_TEXT_EXEMPTIONS.get(needle, ()):
                    continue
                self.assertFalse(needle.lower() in text, f"{path.name} mentions {needle!r}")

    def test_package_contains_only_the_reviewed_modules(self):
        names = sorted(p.relative_to(PACKAGE).as_posix() for p in PACKAGE.rglob("*") if p.is_file()
                       and "__pycache__" not in p.parts)
        self.assertEqual(
            names,
            [
                "__init__.py",
                "__main__.py",
                "api_endpoints.py",
                "api_transport.py",
                "api_vocabulary.py",
                "attempt_store.py",
                "bounded.py",
                "cli.py",
                "credentials.py",
                "endpoints.py",
                "errors.py",
                "incar_tags.py",
                "machine.py",
                "machine_output.py",
                "operations.py",
                "output.py",
                "probes.py",
                "schemas/machine-output-v1.schema.json",
                "schemas/output-v3.schema.json",
                "transport.py",
                "v1_html.py",
                "v1_inputs.py",
                "v1_vocabulary.py",
            ],
        )


class Packaging(unittest.TestCase):
    def test_runtime_dependencies_are_empty(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^dependencies = \[\]$")
        self.assertNotIn("optional-dependencies", text)

    def test_single_console_script(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        section = text.split("[project.scripts]", 1)[1].split("[", 1)[0]
        entries = [line for line in section.strip().splitlines() if line.strip()]
        self.assertEqual(entries, ['bmd-run = "bmd_run.cli:entry_point"'])


if __name__ == "__main__":
    unittest.main()
