"""Information flow for the machine API: no Compute-originated text reaches output.

Like the v1 matrix (``test_output_smuggling.py``), this injects a secret, in
several encodings, into every string (and as an extra key into every object) of
every recorded machine-API response, then renders the client's JSON and human
output. The property checked is not "no secret-looking string" but: every
alphabetic word of output is owned by the client package, and no payload
fragment appears. Values the client relays (digests, job IDs, timestamps,
numbers) are re-rendered after strict pattern checks.
"""

from __future__ import annotations

import copy
import json
import unittest

from _api_support import fixture, new_token
from _support import ROOT, schema_errors
from test_output_smuggling import CLIENT_WORDS, foreign_words, fragments, leaf_strings

import re

from bmd_run import machine, machine_output
from bmd_run.api_transport import ApiResponse
from bmd_run.errors import ClientError

SCHEMA = json.loads((ROOT / "src" / "bmd_run" / "schemas" / "machine-output-v1.schema.json").read_text(encoding="utf-8"))
PLANS = ("plan_si_poscar_energy_only", "plan_nio_poscar_electronic_dos", "plan_si_custom_relax_static", "plan_si_cif_energy_only")
ATTEMPTS = ("attempt_get_submitted", "attempt_get_uncertain", "attempt_submit", "attempt_prepare",
            "attempt_get_labelled_completed")
ERRORS = tuple(
    name for name in (
        "plan_error_structure_invalid", "plan_error_calculation_invalid", "plan_error_invalid_request",
        "plan_error_unknown_desired_output", "plan_error_unauthenticated", "attempt_error_plan_digest_mismatch",
        "attempt_error_submission_uncertain", "attempt_error_active_job_cap", "attempt_error_submit_failed",
    )
)


def encodings(secret: str) -> dict:
    return {
        "plain": secret,
        "split8": " ".join(secret[i:i + 8] for i in range(0, len(secret), 8)),
        "reversed": secret[::-1],
        "interleaved": " ".join(f"PBE {secret[i:i + 6]} Static" for i in range(0, len(secret), 6)),
    }


def paths(value, prefix=()):
    """Paths of every string leaf, and of every object (for extra-key injection)."""

    if isinstance(value, dict):
        yield ("object", prefix)
        for key, item in value.items():
            yield from paths(item, prefix + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from paths(item, prefix + (index,))
    elif isinstance(value, str):
        yield ("string", prefix)


def inject(document, kind, path, payload):
    document = copy.deepcopy(document)
    target = document
    for part in path[:-1] if kind == "string" else path:
        target = target[part]
    if kind == "string":
        target[path[-1]] = payload
    else:
        target[payload] = payload
    return document


def render(kind, body, name):
    """Client output (JSON envelope and text) for one response, or None if refused."""

    case = fixture(name)
    try:
        if kind == "plan":
            result = machine.project_plan(body, case["request"]["body"])
            text = machine_output.render_plan(result)
        elif kind == "attempt":
            attempt_id = case["request"]["path"].rsplit("/", 1)[1]
            result = {"attempt": machine.project_attempt(body, attempt_id=attempt_id, expected_plan_digest=None), "local": None}
            text = machine_output.render_result("status", result)
        else:
            attempt_id = case["request"]["path"].rsplit("/", 1)[1] if "attempts" in case["request"]["path"] else None
            error = machine.classify_error(ApiResponse(case["response"]["status"], body), scope="submit",
                                           attempt_id=attempt_id)
            raise error
    except ClientError as error:
        document = machine_output.envelope(command="status", api_url=None, request=None, result=None, error=error)
        return document, machine_output.to_json(document), machine_output.render_error(error)
    document = machine_output.envelope(command="plan" if kind == "plan" else "status", api_url=None,
                                       request=None, result=result, error=None)
    return document, machine_output.to_json(document), text


# Attempt IDs in output are the client's own (generated, or as asked about) and are checked
# to equal the requested ID; their hex groups are not words.
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


def words(text: str) -> set:
    return foreign_words(UUID.sub(" ", text))


class MachineInformationFlow(unittest.TestCase):
    def check(self, document, json_text, text, forbidden, label):
        self.assertEqual(schema_errors(document, SCHEMA), [], label)
        found = set()
        for string in leaf_strings(document):
            found |= words(string)
        found |= words(text)
        self.assertEqual(found, set(), f"{label}: words not owned by the client")
        for piece in forbidden:
            self.assertNotIn(piece, json_text + text, f"{label}: payload fragment in output")

    def test_unmodified_responses_render_only_client_words(self):
        for kind, names in (("plan", PLANS), ("attempt", ATTEMPTS), ("error", ERRORS)):
            for name in names:
                document, json_text, text = render(kind, fixture(name)["response"]["body"], name)
                self.check(document, json_text, text, (), name)

    def test_compute_prose_is_never_relayed(self):
        for name in ERRORS:
            body = fixture(name)["response"]["body"]
            _, json_text, text = render("error", body, name)
            text = json_text + text
            for key in ("message", "suggestion"):
                if key in body["error"]:
                    self.assertNotIn(body["error"][key], text, name)

    def test_matrix(self):
        secret = new_token()
        total = 0
        for kind, names in (("plan", PLANS), ("attempt", ATTEMPTS), ("error", ERRORS)):
            for name in names:
                body = fixture(name)["response"]["body"]
                for site_kind, path in list(paths(body)):
                    for label, encoded in encodings(secret).items():
                        mutated = inject(body, site_kind, path, encoded)
                        document, json_text, text = render(kind, mutated, name)
                        self.check(document, json_text, text, fragments(secret, encoded), f"{name} {site_kind} {path} {label}")
                        total += 1
        self.assertGreater(total, 2500)
        self.assertFalse({p for p in fragments(secret, secret)} & CLIENT_WORDS)


if __name__ == "__main__":
    unittest.main()
