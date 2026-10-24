"""The client's version is one value everywhere it is reported.

``pyproject.toml`` (the package metadata a wheel carries), ``bmd_run.__version__``,
``bmd-run --version``, the HTTP User-Agent of both transports and ``client.version``
in every versioned JSON document must agree. (Releases v0.1.0 and v0.2.0 both reported
"0.1.0", so an installed version could not identify its release; this keeps it from
recurring.) The installed-wheel metadata is checked in ``test_packaging_smoke``.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import unittest

from _support import ROOT, RecordingNetwork
from test_run_command import RunBase

import bmd_run
from bmd_run import CLIENT_NAME, batch, cli, machine_output, output
from bmd_run.endpoints import Endpoint
from bmd_run.transport import Transport

_PROJECT_VERSION = re.compile(r'(?ms)^\[project\]\s*$.*?^version\s*=\s*"([^"]+)"\s*$')


def project_version() -> str:
    match = _PROJECT_VERSION.search((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert match, "pyproject.toml has no [project] version"
    return match.group(1)


class VersionIdentity(unittest.TestCase):
    def test_package_metadata_and_runtime_version_are_the_same(self):
        self.assertRegex(project_version(), r"^[0-9]+\.[0-9]+\.[0-9]+$")
        self.assertEqual(bmd_run.__version__, project_version())
        self.assertEqual(CLIENT_NAME, "bmd-run")

    def test_cli_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = cli.main(["--version"], stdout=out, stderr=io.StringIO())
            except SystemExit as exit_:
                code = exit_.code
        self.assertEqual((code, out.getvalue().strip()), (0, f"bmd-run {project_version()}"))

    def test_browser_transport_user_agent(self):
        network = RecordingNetwork(lambda method, path, form: (200, "{}"))
        Transport("http://compute.test:8000", connection_factory=network).request(Endpoint.OPENAPI)
        self.assertEqual(network.requests[0]["headers"]["User-Agent"], f"bmd-run/{project_version()}")

    def test_client_version_in_every_json_document(self):
        documents = {
            "output": output.envelope(command="options", base_url=None, request=None, result=None, error=None),
            "machine_output": machine_output.envelope(command="plan", api_url=None, request=None, result=None,
                                                      error=None),
            "run_output": machine_output.run_envelope(api_url=None, request=None,
                                                      progress={"stage": "plan", "attempt_id": None}, error=None),
            "batch_output": batch.envelope(command="batch", api_url=None, manifest=None, options={}, rows=[],
                                           new_attempts=None, error=None),
        }
        for name, document in documents.items():
            self.assertEqual(json.loads(json.dumps(document))["client"],
                             {"name": "bmd-run", "version": project_version()}, name)


class ApiTransportUserAgent(RunBase):
    def test_machine_api_requests_carry_the_same_version(self):
        code, _, err = self.run_cli(self.si, "--json")
        self.assertEqual(code, 0, err)
        agents = {request["headers"].get("User-Agent") for request in self.compute.requests}
        self.assertEqual(agents, {f"bmd-run/{project_version()}"})


if __name__ == "__main__":
    unittest.main()
