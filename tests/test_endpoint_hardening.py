"""Regression tests: same-process callers cannot turn a table entry into another route.

Codex review of eb03c24 showed that ``Endpoint.OPENAPI.path = "/submit"`` made
``Transport.request(Endpoint.OPENAPI)`` send ``GET /submit``. Endpoint members
now carry no route data. Routes are immutable tuples in a read-only private
mapping, and the transport re-checks a literal allowlist.

Python cannot defend against arbitrary code running in the same process (such
code could replace ``http.client`` itself). These tests cover mutation of the
client's public objects and of the private tables a caller can reach.
"""

from __future__ import annotations

import dataclasses
import unittest

from _support import RecordingNetwork

from bmd_run import endpoints
from bmd_run.endpoints import Endpoint, resolve
from bmd_run.errors import UsageError
from bmd_run.transport import Transport

ALLOWED_CALLS = {("GET", "/openapi.json"), ("POST", "/analyze"), ("POST", "/build-workflow")}


def _transport():
    network = RecordingNetwork(lambda m, p, f: (200, "{}"))
    return Transport("http://compute.test:8000", connection_factory=network), network


class CodexMutationExploit(unittest.TestCase):
    def test_exact_codex_exploit_setting_openapi_path_to_submit(self):
        transport, network = _transport()
        try:
            Endpoint.OPENAPI.path = "/submit"
        except AttributeError:
            pass
        else:  # pragma: no cover - would be a regression
            self.fail("Endpoint members must reject attribute assignment")
        transport.request(Endpoint.OPENAPI)
        self.assertEqual(network.calls, [("GET", "/openapi.json")])
        self.assertNotIn("/submit", [path for _, path in network.calls])

    def test_attribute_assignment_and_deletion_are_refused(self):
        for name in ("path", "method", "allowed_fields", "required_fields", "value", "_value_", "_name_"):
            with self.subTest(name=name):
                with self.assertRaises(AttributeError):
                    setattr(Endpoint.OPENAPI, name, "/submit")
                with self.assertRaises(AttributeError):
                    delattr(Endpoint.ANALYZE, name)
        self.assertEqual(resolve(Endpoint.OPENAPI).path, "/openapi.json")

    def test_forced_object_setattr_on_member_fails_closed_or_is_ignored(self):
        transport, network = _transport()
        member = Endpoint.BUILD_WORKFLOW
        original_value = member._value_
        try:
            # Route-like attributes forced onto the member are never read.
            object.__setattr__(member, "path", "/submit")
            object.__setattr__(member, "method", "POST")
            transport.request(member, {"structure": "x", "fmt": "poscar", "workflow": "w"})
            self.assertEqual(network.calls[-1], ("POST", "/build-workflow"))
            # Corrupting the member's identity makes resolution refuse.
            object.__setattr__(member, "_value_", "submit")
            with self.assertRaises(UsageError):
                transport.request(member, {"structure": "x", "fmt": "poscar", "workflow": "w"})
        finally:
            object.__setattr__(member, "_value_", original_value)
            for name in ("path", "method"):
                if name in member.__dict__:
                    del member.__dict__[name]
        self.assertTrue(set(network.calls) <= ALLOWED_CALLS)
        self.assertEqual(len(network.calls), 1)

    def test_route_tuples_cannot_be_modified(self):
        route = resolve(Endpoint.OPENAPI)
        with self.assertRaises(AttributeError):
            route.path = "/submit"
        with self.assertRaises(AttributeError):
            object.__setattr__(route, "path", "/submit")
        with self.assertRaises(TypeError):
            dataclasses.replace(route, path="/submit")  # not a dataclass either
        self.assertEqual(resolve(Endpoint.OPENAPI).path, "/openapi.json")

    def test_private_route_mapping_is_read_only(self):
        with self.assertRaises(TypeError):
            endpoints._ROUTES["openapi"] = endpoints.Route("POST", "/submit", frozenset(), frozenset())
        with self.assertRaises(TypeError):
            del endpoints._ROUTES["openapi"]

    def test_replacing_a_route_object_fails_closed(self):
        transport, network = _transport()
        forged = endpoints.Route("POST", "/submit", frozenset({"structure"}), frozenset())
        saved = endpoints._MEMBERS
        try:
            endpoints._MEMBERS = ((Endpoint.OPENAPI, "openapi", forged),) + saved[1:]
            with self.assertRaises(UsageError):
                transport.request(Endpoint.OPENAPI)
        finally:
            endpoints._MEMBERS = saved
        self.assertEqual(network.requests, [])

    def test_transport_literal_allowlist_blocks_forged_resolution(self):
        """Even if resolution is subverted, the transport's own literal check refuses."""

        transport, network = _transport()
        forged = endpoints.Route("POST", "/submit", frozenset({"structure"}), frozenset())
        import bmd_run.transport as transport_module

        saved = transport_module.resolve
        try:
            transport_module.resolve = lambda endpoint: forged
            with self.assertRaises(UsageError):
                transport.request(Endpoint.OPENAPI)
        finally:
            transport_module.resolve = saved
        self.assertEqual(network.requests, [])

    def test_lookalike_objects_are_refused(self):
        transport, network = _transport()

        class Fake:
            method, path, value, name = "POST", "/submit", "openapi", "OPENAPI"

        for bogus in (Fake(), "openapi", "OPENAPI", ("GET", "/openapi.json"), endpoints.Route(
                "GET", "/openapi.json", frozenset(), frozenset())):
            with self.subTest(bogus=bogus), self.assertRaises(UsageError):
                transport.request(bogus)
        self.assertEqual(network.requests, [])


if __name__ == "__main__":
    unittest.main()
