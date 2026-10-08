"""Static checks that the client package carries no authority beyond its closed endpoint table.

These tests inspect the source and packaging of ``bmd_run``. They prove what
the *client* can do. They cannot prove that a caller is unable to bypass the client;
that is the job of BMD Compute's own access control and of the caller's sandbox.
"""

from __future__ import annotations

import ast
import re
import unittest

from _support import PACKAGE, ROOT

from bmd_run import endpoints

ALLOWED_MODULES = {
    "__future__",
    "argparse",
    "dataclasses",
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
    "sys",
    "types",
    "typing",
    "urllib.parse",
}
# Modules that grant network access may only be imported by the transport.
NETWORK_MODULES = {"http.client", "ssl"}
NETWORK_MODULE_OWNER = "transport.py"

FORBIDDEN_BUILTINS = {"eval", "exec", "compile", "__import__", "open", "breakpoint"}
FORBIDDEN_METHODS = {
    "write_text", "write_bytes", "mkdir", "makedirs", "unlink", "rmdir", "rename",
    "touch", "chmod", "chown", "symlink_to", "open", "system", "popen", "spawnv",
    "execv", "execve", "fork", "kill",
}
ALLOWED_OS_ATTRIBUTES = {"environ"}

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

EXPECTED_ENDPOINTS = (
    ("openapi", "GET", "/openapi.json"),
    ("analyze", "POST", "/analyze"),
    ("build_workflow", "POST", "/build-workflow"),
)


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

    def test_no_route_like_string_outside_the_table(self):
        allowed = {path for _, _, path in EXPECTED_ENDPOINTS}
        route = re.compile(r"^/[a-z][a-z0-9._/-]*$")
        for path in _sources():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if route.match(node.value):
                        self.assertIn(node.value, allowed, f"{path.name}: route-like string {node.value!r}")


class ImportsAndCapabilities(unittest.TestCase):
    def test_only_allowlisted_standard_library_modules_are_imported(self):
        for path in _sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for module in _imported_modules(tree):
                self.assertIn(module, ALLOWED_MODULES, f"{path.name} imports {module}")

    def test_network_modules_are_confined_to_the_transport(self):
        for path in _sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            used = set(_imported_modules(tree)) & NETWORK_MODULES
            if path.name != NETWORK_MODULE_OWNER:
                self.assertFalse(used, f"{path.name} imports network module(s) {used}")

    def test_no_dynamic_execution_process_or_file_writing_calls(self):
        for path in _sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name):
                        self.assertNotIn(node.func.id, FORBIDDEN_BUILTINS, f"{path.name}: {node.func.id}()")
                    if isinstance(node.func, ast.Attribute):
                        self.assertNotIn(node.func.attr, FORBIDDEN_METHODS, f"{path.name}: .{node.func.attr}()")

    def test_os_is_used_only_to_read_the_environment(self):
        for path in _sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
                    self.assertIn(node.attr, ALLOWED_OS_ATTRIBUTES, f"{path.name}: os.{node.attr}")

    def test_forbidden_routes_identity_fields_and_backends_are_not_referenced(self):
        for path in _sources():
            text = path.read_text(encoding="utf-8").lower()
            for needle in FORBIDDEN_TEXT:
                self.assertFalse(needle.lower() in text, f"{path.name} mentions {needle!r}")

    def test_package_contains_only_the_reviewed_modules(self):
        names = sorted(p.relative_to(PACKAGE).as_posix() for p in PACKAGE.rglob("*") if p.is_file()
                       and "__pycache__" not in p.parts)
        self.assertEqual(
            names,
            [
                "__init__.py",
                "__main__.py",
                "bounded.py",
                "cli.py",
                "endpoints.py",
                "errors.py",
                "incar_tags.py",
                "operations.py",
                "output.py",
                "probes.py",
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
