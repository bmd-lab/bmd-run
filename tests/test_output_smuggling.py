"""Adversarial tests: Compute-originated text has no path into Client output.

The property under test is an information-flow property, not a secret detector:

    Every alphabetic word in any output (JSON or human-readable, stdout or
    stderr) must be a word the client itself owns: a word from a string
    constant in the client package, including its closed vocabularies.
    Numbers are permitted. User-supplied request echoes are excluded.

If any server-controlled text could reach output, a word from an injected
payload would violate this. The test does not try to recognise encodings
of secrets. It injects fragmented, escaped, reordered and re-encoded
payloads into every Compute field the adapter still reads, and into fields it
ignores, then checks the property and that no payload fragment appears.

Also covered:

* Codex review findings: the whole token in ``workflow_spec_json``
  (first review), and the split-token and split-UUID attacks (second review).
* The residual numeric channel, documented in its own test.
"""

from __future__ import annotations

import ast
import base64
import html as html_lib
import json
import re
import unittest
from pathlib import Path

from _support import (
    FIXTURES,
    PACKAGE,
    TOKEN_RE,
    RecordingNetwork,
    fixture_text,
    load_schema,
    run_cli,
    schema_errors,
    v1_router,
)

from bmd_run import errors

SCHEMA = load_schema()
SI_FILE = str(FIXTURES / "Si.POSCAR")
NIO_FILE = str(FIXTURES / "NiO.POSCAR")
DOS = "build_si_electronic_dos.html"
NIO_DOS = "build_nio_electronic_dos.html"
BAND = "build_si_electronic_band_structure.html"
SPEC_RE = re.compile(r'(name="workflow_spec_json"\s+value=")([^"]*)(")')
UUID_ATTR_RE = re.compile(r'name="submission_attempt_id" value="([^"]+)"')

NUMBER = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
WORD = re.compile(r"[^\W\d_](?:[^\W_]|_)*")


def _client_words() -> frozenset:
    words = set()
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                words.update(WORD.findall(NUMBER.sub(" ", node.value)))
    return frozenset(words)


CLIENT_WORDS = _client_words()


HEX_DIGEST = re.compile(r"\b[0-9a-f]{64}\b")  # client-computed SHA-256 digests (identity, catalogue)


def foreign_words(text: str) -> set:
    text = NUMBER.sub(" ", HEX_DIGEST.sub(" ", text))
    return {w for w in WORD.findall(text) if w not in CLIENT_WORDS}


def leaf_strings(value):
    """Every key and string value in a JSON document."""

    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from leaf_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from leaf_strings(item)
    elif isinstance(value, str):
        yield value


def strip_user_echo(document: dict) -> dict:
    """Remove the parts of a JSON document that echo the user's own input."""

    document = dict(document)
    document["request"] = None
    document["compute"] = dict(document["compute"], base_url=None)
    return document


# --- payload construction ----------------------------------------------------------


def secrets_from(page_html: str):
    token = TOKEN_RE.search(page_html).group(1)
    uuid = UUID_ATTR_RE.search(page_html).group(1)
    return {"token": token, "uuid": uuid}


def encodings(secret: str):
    raw = secret.encode()
    return {
        "plain": secret,
        "split8_spaces": " ".join(secret[i:i + 8] for i in range(0, len(secret), 8)),
        "split4_newlines": "\n".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
        "uuid_groups": " ".join(secret.split("-")),
        "html_entities": "".join(f"&#x{ord(c):x};" for c in secret),
        "percent": "".join(f"%{b:02X}" for b in raw),
        "reversed": secret[::-1],
        "hex": raw.hex(),
        "base32_upper": base64.b32encode(raw).decode().rstrip("="),
        "interleaved": " ".join(f"PBE {secret[i:i + 6]} Static" for i in range(0, len(secret), 6)),
        "zero_width": "​".join(secret),
    }


def fragments(secret: str, encoded: str) -> set:
    """Distinctive pieces of the payload that must never appear in output."""

    pieces = set(re.findall(r"[A-Za-z0-9]{4,}", secret))
    pieces |= {secret[i:i + 6] for i in range(0, len(secret) - 5, 6)}
    pieces |= set(re.findall(r"[A-Za-z0-9]{6,}", html_lib.unescape(encoded)))
    return {p for p in pieces if len(p) >= 4 and p not in CLIENT_WORDS and not p.isdigit()}


# --- injection sites -------------------------------------------------------------------


def mutate_spec(page_html, mutate):
    def replace(match):
        spec = json.loads(html_lib.unescape(match.group(2)))
        mutate(spec)
        return match.group(1) + html_lib.escape(json.dumps(spec), quote=True) + match.group(3)

    out, count = SPEC_RE.subn(replace, page_html)
    assert count
    return out


def mutate_catalogue(page_html, mutate):
    anchor = 'id="calculation-stage-options">'
    start = page_html.index(anchor) + len(anchor)
    end = page_html.index("</script>", start)
    catalogue = json.loads(page_html[start:end])
    mutate(catalogue)
    return page_html[:start] + json.dumps(catalogue).replace("</", "<\\/") + page_html[end:]


def after(anchor, marker, *, raw=False, newline=False):
    """Insert text right after ``marker`` (first occurrence following ``anchor``)."""

    def inject(page_html, payload):
        text = payload if raw else html_lib.escape(payload, quote=False)
        if newline:
            text = text + "\n"
        start = page_html.index(anchor)
        position = page_html.index(marker, start) + len(marker)
        return page_html[:position] + text + page_html[position:]
    return inject


def replace_cell(label, *, raw=False):
    """Replace a two-column table row value ``<td>label</td><td ...>VALUE</td>``."""

    pattern = re.compile(r"(<td>" + re.escape(label) + r"</td>\s*<td[^>]*>)([^<]*)(</td>)")

    def inject(page_html, payload):
        text = payload if raw else html_lib.escape(payload, quote=False)
        out, count = pattern.subn(lambda m: m.group(1) + text + m.group(3), page_html, count=1)
        assert count, label
        return out
    return inject


def replace_metric(label):
    pattern = re.compile(r'(<div class="metric-label">' + re.escape(label) +
                         r'</div>\s*<div class="metric-value">)([^<]*)(</div>)')

    def inject(page_html, payload):
        out, count = pattern.subn(lambda m: m.group(1) + html_lib.escape(payload, quote=False) + m.group(3),
                                  page_html, count=1)
        assert count, label
        return out
    return inject


def spec_set(path_fn):
    return lambda h, p: mutate_spec(h, lambda s: path_fn(s, p))


def cat_set(path_fn):
    return lambda h, p: mutate_catalogue(h, lambda c: path_fn(c, p))


def incar_line(prefix="", suffix=""):
    return lambda h, p: after('class="tab-panel tab-panel-incar"', '<pre class="input-preview">',
                              raw=False)(h, prefix + p.replace("\n", " ") + suffix + "\n")


PLAN = ["plan", "dos", SI_FILE]
PLAN_NIO = ["plan", "dos", NIO_FILE]
PLAN_BAND = ["plan", "bands", SI_FILE]
ANALYZE = ["analyze", SI_FILE]
ANALYZE_NIO = ["analyze", NIO_FILE]
OPTIONS = ["options"]
CPUS7 = ["plan", "energy", SI_FILE, "--cpus", "7"]

# (site name, fixture, argv, injector)
SITES = [
    # workflow_spec_json
    ("spec.recipe", DOS, PLAN, spec_set(lambda s, p: s.__setitem__("recipe", p))),
    ("spec.stage_type", DOS, PLAN, spec_set(lambda s, p: s["stages"][0].__setitem__("stage_type", p))),
    ("spec.theory", DOS, PLAN, spec_set(lambda s, p: s["stages"][1].__setitem__("theory", p))),
    ("spec.modifiers", DOS, PLAN, spec_set(lambda s, p: s["stages"][2].__setitem__("modifiers", p.split()))),
    ("spec.options", NIO_DOS, PLAN_NIO,
     spec_set(lambda s, p: s["stages"][0]["options"]["dft_u"].__setitem__("parameter_source", p))),
    ("spec.label", DOS, PLAN, spec_set(lambda s, p: s.__setitem__("label", p))),
    ("spec.extra", DOS, PLAN, spec_set(lambda s, p: s.__setitem__("submission_identity_token", p))),
    ("analyze.spec.theory", "analyze_si.html", ANALYZE,
     spec_set(lambda s, p: s["stages"][0].__setitem__("theory", p))),
    # option catalogue
    ("cat.value", "analyze_si.html", OPTIONS, cat_set(lambda c, p: c["theories"][0].__setitem__("value", p))),
    ("cat.label", "analyze_si.html", OPTIONS, cat_set(lambda c, p: c["desired_outputs"][2].__setitem__("label", p))),
    ("cat.description", "analyze_si.html", OPTIONS,
     cat_set(lambda c, p: c["desired_outputs"][0].__setitem__("description", p))),
    ("cat.enabled", "analyze_si.html", OPTIONS, cat_set(lambda c, p: c["modifiers"][0].__setitem__("enabled", p))),
    ("cat.tooltip", "analyze_si.html", OPTIONS, cat_set(lambda c, p: c["modifiers"][0].__setitem__("tooltip", p))),
    ("cat.nested_stage", "analyze_si.html", OPTIONS,
     cat_set(lambda c, p: c["desired_outputs"][1]["workflow_spec"]["stages"][0].__setitem__("stage_type", p))),
    ("cat.extra_entry", "analyze_si.html", OPTIONS,
     cat_set(lambda c, p: c["stage_types"].append({"value": p, "label": p}))),
    # structure facts
    ("structure.formula", "analyze_si.html", ANALYZE, replace_metric("Formula")),
    ("structure.reduced", "analyze_si.html", ANALYZE, replace_metric("Reduced")),
    ("structure.atoms", "analyze_si.html", ANALYZE, replace_metric("Atoms")),
    ("structure.volume", "analyze_si.html", ANALYZE, replace_metric("Volume")),
    ("structure.density", DOS, PLAN, replace_metric("Density")),
    ("structure.a", "analyze_si.html", ANALYZE, replace_cell("a (Å)")),
    ("structure.alpha", DOS, PLAN, replace_cell("α (degrees)")),
    ("structure.sg_number", "analyze_si.html", ANALYZE, replace_cell("Space Group Number")),
    ("structure.crystal_system", "analyze_si.html", ANALYZE, replace_cell("Crystal System")),
    ("structure.sg_symbol_ignored", "analyze_si.html", ANALYZE, replace_cell("Space Group Symbol")),
    ("structure.headline_ignored", "analyze_si.html", ANALYZE,
     after("Structure Summary", '<div class="summary-line">')),
    # method considerations
    ("consideration.id", "analyze_nio.html", ANALYZE_NIO,
     lambda h, p: h.replace('data-method-consideration-id="spin.composition_screen"',
                            f'data-method-consideration-id="{html_lib.escape(p, quote=True)}"', 1)),
    ("consideration.title", "analyze_nio.html", ANALYZE_NIO,
     after('data-method-consideration-id="spin.composition_screen"', "<span>Spin Polarisation applied")),
    ("consideration.summary", "analyze_nio.html", ANALYZE_NIO,
     after('data-method-consideration-id="spin.composition_screen"', "Ni detected.")),
    ("consideration.title_entities", NIO_DOS, PLAN_NIO,
     after('data-method-consideration-id="dftu.mp_oxide_fluoride"', "<span>", raw=True)),
    # execution / resources tables
    ("execution.stage_vasp", DOS, PLAN, replace_cell("Stage 1 VASP")),
    ("execution.stage_name_ignored", DOS, PLAN, replace_cell("Stage 2")),
    ("execution.run_directory_ignored", DOS, PLAN, replace_cell("Run directory")),
    ("resources.partition", DOS, PLAN, replace_cell("Partition")),
    ("resources.account", DOS, PLAN, replace_cell("Account")),
    ("resources.cpus", DOS, PLAN, replace_cell("Tasks / CPUs")),
    ("resources.memory", DOS, PLAN, replace_cell("Memory")),
    ("resources.walltime", DOS, PLAN, replace_cell("Walltime")),
    ("resources.job_name_ignored", DOS, PLAN, replace_cell("Job name")),
    ("calculation_plan_ignored", DOS, PLAN, after("Calculation Plan</div>", '<div class="callout-value">')),
    ("scripts_ignored", DOS, PLAN, after("Exact BMD Submission Script", '<pre class="input-preview">')),
    # INCAR / KPOINTS previews
    ("incar.bare_line", DOS, PLAN, incar_line()),
    ("incar.comment_line", DOS, PLAN, incar_line(prefix="# ")),
    ("incar.unknown_tag_name", DOS, PLAN,
     lambda h, p: after('class="tab-panel tab-panel-incar"', "ENCUT = 580.0\n")(
         h, "\n".join(f"{re.sub('[^A-Z0-9]', '', w.upper())[:30] or 'X'} = 1" for w in p.split()[:20]) + "\n")),
    ("incar.word_value", DOS, PLAN, lambda h, p: h.replace("ALGO = Fast", "ALGO = " + html_lib.escape(p.split()[0]), 1)),
    ("incar.opaque_tag", DOS, PLAN,
     after('class="tab-panel tab-panel-incar"', "ENCUT = 580.0\n", raw=False,)),
    ("incar.value_suffix", DOS, PLAN,
     lambda h, p: h.replace("ENCUT = 580.0", "ENCUT = 580.0 " + html_lib.escape(p.replace("\n", " ")), 1)),
    ("incar.stage_header_text", DOS, PLAN,
     lambda h, p: h.replace("# Stage 2 - Static Energy (HSE06)",
                            "# Stage 2 - " + html_lib.escape(p.replace("\n", " ")), 1)),
    ("incar.executable_header", DOS, PLAN,
     lambda h, p: h.replace("# VASP executable - vasp_std", "# VASP executable - " + html_lib.escape(p.split()[0]), 1)),
    ("kpoints.comment_ignored", DOS, PLAN,
     lambda h, p: h.replace("pymatgen with grid density = 793 / number of atoms",
                            html_lib.escape(p.replace("\n", " ")), 1)),
    ("kpoints.mode", DOS, PLAN, lambda h, p: h.replace("0\nGamma\n7 7 7", "0\n" + html_lib.escape(p.split()[0]) + "\n7 7 7", 1)),
    ("kpoints.explicit_label", BAND, PLAN_BAND,
     lambda h, p: h.replace("0.0 0.0 0.0 0 \\Gamma", "0.0 0.0 0.0 0 " + html_lib.escape(p.replace("\n", " ")), 1)),
    # Compute error panels
    ("error.title", "build_si_badcpus.html", CPUS7,
     after('<div class="prep-title">', "Calculation Validation Failed")),
    ("error.reason", "build_si_badcpus.html", CPUS7, after("<td>Reason</td>", "<td>")),
    ("error.suggestion", "build_si_badcpus.html", CPUS7, after("<td>Suggestion</td>", "<td>")),
    # page furniture
    ("hidden_textarea", DOS, PLAN,
     lambda h, p: h.replace("</body>", f'<textarea class="hidden-field" name="monitor_state_json">'
                                       f'{html_lib.escape(p)}</textarea></body>')),
]
# "incar.opaque_tag" inserts a free-text SYSTEM tag carrying the payload.
SITES = [
    (name, fixture, argv,
     (lambda h, p, _inject=inject: _inject(h, "SYSTEM = " + p.replace("\n", " "))) if name == "incar.opaque_tag"
     else inject)
    for name, fixture, argv, inject in SITES
]


def serve(fixture_name: str, mutated_html: str):
    original = fixture_text(fixture_name)

    def router(method, path, form):
        answer = v1_router(method, path, form)
        if answer[1] == original:
            return (answer[0], mutated_html) + tuple(answer[2:])
        return answer

    return RecordingNetwork(router)


def run_both(argv, network_factory):
    """Run in JSON and text mode. Returns [(mode, code, combined_output, json_doc_or_None)]."""

    results = []
    for mode in ("json", "text"):
        code, out, err, _ = run_cli((["--json"] if mode == "json" else []) + argv, network_factory())
        document = json.loads(out) if mode == "json" else None
        results.append((mode, code, out + err, document))
    return results


class FlowChecks(unittest.TestCase):
    def assert_no_server_text(self, mode, code, text, document, *, forbidden=()):
        if document is not None:
            self.assertEqual(schema_errors(document, SCHEMA), [])
            found = set()
            for string in leaf_strings(strip_user_echo(document)):
                found |= foreign_words(string)
        else:
            found = foreign_words(text)
        self.assertEqual(found, set(), f"{mode}: foreign words in output")
        for piece in forbidden:
            self.assertNotIn(piece, text, f"{mode}: payload fragment in output")


class BaselinesUseOnlyClientWords(FlowChecks):
    def test_unmodified_outputs(self):
        for argv in (["identity"], OPTIONS, ANALYZE, ANALYZE_NIO, ["plan", "energy", SI_FILE],
                     ["plan", "relax", SI_FILE], PLAN, PLAN_BAND, PLAN_NIO, CPUS7):
            with self.subTest(argv=argv):
                for mode, code, text, document in run_both(argv, RecordingNetwork):
                    self.assertIn(code, (0, 1))
                    self.assert_no_server_text(mode, code, text, document)


class CodexRegressions(FlowChecks):
    def test_first_review_token_in_workflow_spec(self):
        original = fixture_text(DOS)
        token = secrets_from(original)["token"]

        def inject(spec):
            spec["submission_identity_token"] = token
            spec["stages"][0]["options"] = {"submission_identity_token": token}

        mutated = mutate_spec(original, inject)
        for mode, code, text, document in run_both(PLAN, lambda: serve(DOS, mutated)):
            self.assertEqual(code, errors.EXIT_OK)
            self.assert_no_server_text(mode, code, text, document, forbidden=fragments(token, token))

    def test_second_review_split_token_in_consideration_prose(self):
        original = fixture_text(NIO_DOS)
        token = secrets_from(original)["token"]
        split = " ".join(token[i:i + 8] for i in range(0, len(token), 8))
        mutated = after('<p class="method-consideration-summary">', "detected.")(original, " " + split)
        baseline = {mode: text for mode, _, text, _ in run_both(PLAN_NIO, RecordingNetwork)}
        for mode, code, text, document in run_both(PLAN_NIO, lambda: serve(NIO_DOS, mutated)):
            self.assertEqual(code, errors.EXIT_OK)
            self.assertEqual(text, baseline[mode])  # prose is not read at all
            self.assert_no_server_text(mode, code, text, document, forbidden=fragments(token, split))

    def test_second_review_split_uuid_in_incar_preview(self):
        original = fixture_text(NIO_DOS)
        uuid = secrets_from(original)["uuid"]
        mutated = after('class="tab-panel tab-panel-incar"', '<pre class="input-preview">')(
            original, "# " + " ".join(uuid.split("-")) + "\n")
        for mode, code, text, document in run_both(PLAN_NIO, lambda: serve(NIO_DOS, mutated)):
            self.assertEqual(code, errors.EXIT_UNEXPECTED_RESPONSE)
            self.assert_no_server_text(mode, code, text, document, forbidden=set(uuid.split("-")))


class AdversarialMatrix(FlowChecks):
    """Every remaining Compute field x every encoding x token and UUID, in JSON and text mode."""

    def test_matrix(self):
        runs = 0
        for site, fixture, argv, inject in SITES:
            original = fixture_text(fixture)
            secrets = secrets_from(original if TOKEN_RE.search(original) else fixture_text(DOS))
            baseline_code = run_cli(argv, RecordingNetwork())[0]
            for secret_name, secret in secrets.items():
                for encoding, payload in encodings(secret).items():
                    with self.subTest(site=site, secret=secret_name, encoding=encoding):
                        mutated = inject(original, payload)
                        self.assertNotEqual(mutated, original)
                        forbidden = fragments(secret, payload)
                        for mode, code, text, document in run_both(argv, lambda: serve(fixture, mutated)):
                            self.assertIn(code, {baseline_code, errors.EXIT_UNEXPECTED_RESPONSE},
                                          f"{site}/{encoding}/{mode}: exit {code}")
                            self.assert_no_server_text(mode, code, text, document, forbidden=forbidden)
                        runs += 1
        self.assertGreaterEqual(runs, len(SITES) * 2 * 10)

    def test_identity_relays_nothing_from_openapi(self):
        document = json.loads(fixture_text("openapi.json"))
        secret = secrets_from(fixture_text(DOS))["token"]
        for encoding, payload in encodings(secret).items():
            with self.subTest(encoding=encoding):
                mutated = dict(document)
                mutated["paths"] = dict(document["paths"], **{"/x" + payload[:40]: {"get": {"summary": payload}}})
                body = json.dumps(mutated)
                network = lambda: RecordingNetwork(  # noqa: E731
                    lambda m, p, f: (200, body) if p == "/openapi.json" else v1_router(m, p, f))
                for mode, code, text, doc in run_both(["identity"], network):
                    self.assertEqual(code, errors.EXIT_OK)
                    self.assert_no_server_text(mode, code, text, doc, forbidden=fragments(secret, payload))


class ResidualNumericChannel(unittest.TestCase):
    """Documents the accepted residual: Compute-chosen numbers within bounds are reported as numbers.

    A deliberately malicious Compute could encode data in the numeric values it
    emits (for example, INCAR values or lattice parameters). Each value is
    limited to 12 significant digits and rendered by the client. This is a
    bounded numeric covert channel, not a text relay. Closing it would require
    not reporting any Compute-derived numbers at all.
    """

    def test_numeric_values_are_rendered_by_the_client(self):
        original = fixture_text(DOS)
        mutated = original.replace("ENCUT = 580.0", "ENCUT = 123456.789", 1)
        code, out, err, _ = run_cli(["--json"] + PLAN, serve(DOS, mutated))
        self.assertEqual(code, 0)
        tags = json.loads(out)["result"]["generated_inputs"]["stages"][0]["incar"]["tags"]
        self.assertIn({"tag": "ENCUT", "value": 123456.789}, tags)
        too_precise = original.replace("ENCUT = 580.0", "ENCUT = 580.0000000000001", 1)
        self.assertEqual(run_cli(PLAN, serve(DOS, too_precise))[0], errors.EXIT_UNEXPECTED_RESPONSE)


if __name__ == "__main__":
    unittest.main()
