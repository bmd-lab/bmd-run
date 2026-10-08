"""The transport can only reach the configured origin, only through the endpoint table."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from _support import RecordingNetwork

from bmd_run.endpoints import Endpoint
from bmd_run.errors import ComputeUnreachable, UnexpectedResponse, UsageError
from bmd_run.transport import (
    MAX_RESPONSE_BYTES,
    Transport,
    default_connection_factory,
    parse_origin,
)


def _transport(router):
    network = RecordingNetwork(router)
    return Transport("http://compute.test:8000", connection_factory=network), network


class OriginValidation(unittest.TestCase):
    def test_accepts_bare_origins(self):
        self.assertEqual(parse_origin("http://compute.test:8000").url, "http://compute.test:8000")
        self.assertEqual(parse_origin("https://compute.test/").url, "https://compute.test")

    def test_rejects_everything_else(self):
        for url in [
            "",
            "compute.test:8000",
            "file:///etc/passwd",
            "ftp://compute.test",
            "ssh://power.tau.ac.il",
            "http://user:pw@compute.test",
            "http://compute.test/prefix",
            "http://compute.test/?q=1",
            "http://compute.test/#frag",
            "http://:8000",
            "http://compute.test:notaport",
        ]:
            with self.subTest(url=url), self.assertRaises(UsageError):
                parse_origin(url)


class ClosedTable(unittest.TestCase):
    def test_only_endpoint_members_are_accepted(self):
        transport, network = _transport(lambda m, p, f: (200, "{}"))
        for bogus in ["/submit", ("POST", "/submit"), None, "OPENAPI"]:
            with self.subTest(bogus=bogus), self.assertRaises(UsageError):
                transport.request(bogus)  # type: ignore[arg-type]
        self.assertEqual(network.requests, [])

    def test_unknown_or_missing_form_fields_are_refused_before_sending(self):
        transport, network = _transport(lambda m, p, f: (200, ""))
        cases = [
            (Endpoint.ANALYZE, {"structure": "x", "fmt": "poscar", "monitor_state_json": "{}"}),
            (Endpoint.BUILD_WORKFLOW, {"structure": "x", "fmt": "poscar", "workflow": "energy_only",
                                       "workflow_spec_json": "{}"}),
            (Endpoint.BUILD_WORKFLOW, {"structure": "x", "fmt": "poscar", "workflow": "energy_only",
                                       "submission_identity_token": "t"}),
            (Endpoint.BUILD_WORKFLOW, {"structure": "x", "fmt": "poscar"}),
            (Endpoint.OPENAPI, {"anything": "x"}),
            (Endpoint.ANALYZE, {"structure": "x", "fmt": 3}),
        ]
        for endpoint, form in cases:
            with self.subTest(form=form), self.assertRaises(UsageError):
                transport.request(endpoint, form)
        self.assertEqual(network.requests, [])

    def test_method_and_path_come_only_from_the_table(self):
        transport, network = _transport(lambda m, p, f: (200, "{}"))
        transport.request(Endpoint.OPENAPI)
        transport.request(Endpoint.ANALYZE, {"structure": "x", "fmt": "poscar"})
        transport.request(Endpoint.BUILD_WORKFLOW, {"structure": "x", "fmt": "poscar", "workflow": "w"})
        self.assertEqual(
            network.calls,
            [("GET", "/openapi.json"), ("POST", "/analyze"), ("POST", "/build-workflow")],
        )
        self.assertTrue(all(origin.host == "compute.test" for origin in network.origins))


class ResponseHandling(unittest.TestCase):
    def test_redirects_are_never_followed(self):
        transport, network = _transport(lambda m, p, f: (302, "", {"Location": "http://elsewhere/"}))
        with self.assertRaises(UnexpectedResponse):
            transport.request(Endpoint.OPENAPI)
        self.assertEqual(len(network.requests), 1)

    def test_oversized_responses_are_refused(self):
        transport, _ = _transport(lambda m, p, f: (200, "x" * (MAX_RESPONSE_BYTES + 10)))
        with self.assertRaises(UnexpectedResponse):
            transport.request(Endpoint.OPENAPI)

    def test_network_failure_is_reported_as_unreachable(self):
        def refuse(origin, timeout):
            raise ConnectionRefusedError("refused")

        transport = Transport("http://compute.test:8000", connection_factory=refuse)
        with self.assertRaises(ComputeUnreachable):
            transport.request(Endpoint.OPENAPI)

    def test_default_connection_ignores_proxy_environment(self):
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://proxy.evil:3128",
                                          "HTTPS_PROXY": "http://proxy.evil:3128"}):
            for scheme in ("http", "https"):
                connection = default_connection_factory(parse_origin(f"{scheme}://compute.test:8000"), 5)
                self.assertEqual(connection.host, "compute.test")
                self.assertEqual(connection.port, 8000)
                self.assertIsNone(getattr(connection, "_tunnel_host", None))

    def test_identifies_itself_and_closes_connection(self):
        transport, network = _transport(lambda m, p, f: (200, "{}"))
        transport.request(Endpoint.OPENAPI)
        headers = network.requests[0]["headers"]
        self.assertTrue(headers["User-Agent"].startswith("bmd-run/"))
        self.assertEqual(headers["Connection"], "close")


if __name__ == "__main__":
    unittest.main()
