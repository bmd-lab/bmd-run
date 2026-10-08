"""The four read/build-only operations: identity, options, analyze, plan.

Each operation sends requests only through :class:`transport.Transport` (the
closed endpoint table). Each returns a Client-owned ``dict`` whose strings are
client constants or vocabulary members, and whose numbers were parsed within
bounds. No Compute-originated text is ever placed in a result.
"""

from __future__ import annotations

import hashlib
import json
from typing import List, Mapping, Optional

from . import probes
from . import v1_vocabulary as vocab
from .endpoints import Endpoint
from .errors import ComputeRejected, UnexpectedResponse
from .transport import Response, Transport
from .v1_html import Page
from .v1_inputs import render_kpoints

IDENTITY_STATEMENT = (
    "BEHAVIOURAL FINGERPRINT ONLY - NOT VERIFIED SOURCE IDENTITY. "
    "BMD Compute v1.0.0 exposes no deployed source identity over HTTP. This compares "
    "how the service answers fixed probes with a recording made from a clean local "
    "v1.0.0 checkout. It cannot establish the commit, tag, working-tree state or "
    "configuration of the running service."
)

PLAN_IS_PREVIEW_NOTE = (
    "Preview only. Nothing was prepared or submitted; this client has no "
    "prepare or submit capability."
)

INPUTS_NOTE = (
    "INCAR and KPOINTS are rebuilt by the client from values parsed out of BMD Compute's "
    "preview. Tags outside the client's closed INCAR vocabulary, free-text tags, comments "
    "and explicit k-point coordinates are not shown."
)

# Fields BMD Compute v1 shows in its browser page that the client does not report, because
# v1 offers them only as free text with no bounded machine representation.
UNAVAILABLE_ANALYZE = (
    "method_consideration_text",
    "structure_summary_headline",
    "space_group_symbol",
)
UNAVAILABLE_PLAN = UNAVAILABLE_ANALYZE + (
    "compute_stage_display_text",
    "calculation_plan_text",
    "incar_comments",
    "opaque_incar_tags",
    "kpoints_comments",
    "explicit_kpoint_coordinates",
    "poscar_preview",
    "submission_scripts",
    "execution_paths_and_modules",
)
ERROR_STAGE_MESSAGES = {
    "structure_validation": "BMD Compute rejected the structure.",
    "calculation_validation": "BMD Compute rejected the calculation request.",
    "unrecognized": "BMD Compute rejected the request.",
}
ERROR_SUGGESTION = (
    "BMD Compute v1 explains the reason only as free text in its web page, which this client "
    "does not relay. Check the structure, Desired Output and resource values you sent, or open "
    "BMD Compute in a browser to read its explanation."
)

_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


def canonical_sha256(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- shared response handling ----------------------------------------------------


def _page(response: Response) -> Page:
    """Return the page for a successful 200 response; raise for anything else."""

    if response.status in (200, 400):
        page = Page(response.text)
        stage = page.failure_stage()
        if stage is None and response.status == 400:
            stage = "unrecognized"
        if stage is not None:
            raise ComputeRejected(
                ERROR_STAGE_MESSAGES[stage],
                suggestion=ERROR_SUGGESTION,
                http_status=int(response.status),
                compute_stage=stage,
            )
        return page
    if response.status == 422:
        raise UnexpectedResponse(
            "BMD Compute rejected the form encoding (HTTP 422). This indicates a client/Compute "
            "contract mismatch, not a scientific validation result.",
            http_status=422,
        )
    raise UnexpectedResponse(
        f"BMD Compute answered {response.route.method} {response.route.path} with HTTP "
        f"{int(response.status)}.",
        suggestion="This is a Compute-side error; the request was not completed.",
        http_status=int(response.status),
    )


def _post_analyze(transport: Transport, structure: str, fmt: str) -> Page:
    return _page(transport.request(Endpoint.ANALYZE, {"structure": structure, "fmt": fmt}))


def _post_build(
    transport: Transport,
    structure: str,
    fmt: str,
    desired_output: str,
    resources: Optional[Mapping[str, str]] = None,
) -> Page:
    form = {"structure": structure, "fmt": fmt, "workflow": desired_output}
    for key, value in (resources or {}).items():
        if value is not None:
            form[key] = value
    return _page(transport.request(Endpoint.BUILD_WORKFLOW, form))


def _openapi_surface_sha256(transport: Transport) -> str:
    """Hash of the advertised route/field surface. Only the hash leaves this function."""

    response = transport.request(Endpoint.OPENAPI)
    if response.status != 200:
        raise UnexpectedResponse(
            f"BMD Compute answered GET /openapi.json with HTTP {int(response.status)}.",
            http_status=int(response.status),
        )
    try:
        document = json.loads(response.text)
        schemas = document.get("components", {}).get("schemas", {})
        surface = []
        for path, operations in sorted(document["paths"].items()):
            for method, operation in sorted(operations.items()):
                if method not in _HTTP_METHODS:
                    continue
                fields: List[str] = []
                content = (operation.get("requestBody") or {}).get("content") or {}
                for media in content.values():
                    reference = (media.get("schema") or {}).get("$ref", "")
                    name = reference.rsplit("/", 1)[-1]
                    fields.extend(sorted((schemas.get(name) or {}).get("properties", {})))
                surface.append({"method": method.upper(), "path": path, "form_fields": sorted(set(fields))})
    except (ValueError, KeyError, AttributeError, TypeError) as exc:
        raise UnexpectedResponse("BMD Compute /openapi.json has an unexpected shape.") from exc
    return canonical_sha256(surface)


# --- operations -----------------------------------------------------------------


def identity(transport: Transport) -> dict:
    """Behavioural fingerprint. Output contains only client-computed hashes and constants."""

    observed = {"openapi_surface": _openapi_surface_sha256(transport)}

    analyze_page = _post_analyze(transport, probes.PROBE_STRUCTURE, probes.PROBE_STRUCTURE_FORMAT)
    observed["option_catalogue"] = canonical_sha256(analyze_page.raw_option_catalogue())

    build_page = _post_build(
        transport, probes.PROBE_STRUCTURE, probes.PROBE_STRUCTURE_FORMAT, probes.PROBE_DESIRED_OUTPUT
    )
    previews = build_page.raw_input_previews()
    observed["probe_energy_plan"] = canonical_sha256(
        {
            "workflow_spec": build_page.raw_workflow_spec(),
            "incar": previews["incar"],
            "kpoints": previews["kpoints"],
            "poscar": previews["poscar"],
            "resources": build_page.raw_resource_rows(),
        }
    )

    probes_out = {
        name: {
            "observed_sha256": observed[name],
            "reference_sha256": probes.REFERENCE_FINGERPRINTS[name],
            "matches_reference": observed[name] == probes.REFERENCE_FINGERPRINTS[name],
        }
        for name in ("openapi_surface", "option_catalogue", "probe_energy_plan")
    }
    return {
        "kind": "behavioural_fingerprint",
        "verified_source_identity": False,
        "statement": IDENTITY_STATEMENT,
        "reference": {
            "release": probes.REFERENCE_RELEASE,
            "commit": probes.REFERENCE_COMMIT,
            "recorded_from": "clean local BMD Compute checkout run with the pinned scientific runtime",
        },
        "algorithm": probes.FINGERPRINT_ALGORITHM,
        "probes": probes_out,
        "matches_reference": all(item["matches_reference"] for item in probes_out.values()),
    }


def options(transport: Transport) -> dict:
    page = _post_analyze(transport, probes.PROBE_STRUCTURE, probes.PROBE_STRUCTURE_FORMAT)
    return {
        "source": (
            "BMD Compute calculation_form_options() as rendered on the v1 Analyze page "
            "(obtained by analyzing the client's fixed probe structure); values recognised "
            "against the client's closed v1.0.0 vocabulary, labels are the client's own"
        ),
        "catalogue_sha256": canonical_sha256(page.raw_option_catalogue()),
        "catalogue": page.option_catalogue(),
    }


def analyze(transport: Transport, structure: str, fmt: str) -> dict:
    page = _post_analyze(transport, structure, fmt)
    return {
        "structure": page.structure(),
        "method_considerations": page.considerations(),
        "default_workflow": page.workflow(),
        "unavailable_from_v1_machine_interface": list(UNAVAILABLE_ANALYZE),
    }


def plan(
    transport: Transport,
    structure: str,
    fmt: str,
    desired_output: str,
    resources: Optional[Mapping[str, str]] = None,
) -> dict:
    page = _post_build(transport, structure, fmt, desired_output, resources)
    workflow = page.workflow()
    requested = vocab.DESIRED_OUTPUTS[vocab.DESIRED_OUTPUTS.index(desired_output)] \
        if desired_output in vocab.DESIRED_OUTPUTS else None
    if requested is None or workflow["desired_output"] != requested:
        raise UnexpectedResponse(
            "BMD Compute resolved a different Desired Output from the one requested. "
            "Refusing to report this plan.",
        )
    stage_count = len(workflow["stages"])
    executables = page.stage_executables(stage_count)
    inputs = page.input_stages(stage_count)
    stages = []
    for stage, stage_inputs in zip(workflow["stages"], inputs):
        executable = executables[stage["index"]]
        header_executable = stage_inputs["header_vasp_executable"]
        if header_executable is not None and executable is not None and header_executable != executable:
            raise UnexpectedResponse("BMD Compute preview and execution summary disagree on a VASP executable.")
        stages.append({**stage, "vasp_executable": executable or header_executable})
    return {
        "resolved_workflow": {
            "desired_output": workflow["desired_output"],
            "stage_count": stage_count,
            "stages": stages,
        },
        "resources": page.resources(),
        "status": PLAN_IS_PREVIEW_NOTE,
        "method_considerations": page.considerations(),
        "structure": page.structure(),
        "generated_inputs": {
            "note": INPUTS_NOTE,
            "stages": [
                {
                    "index": item["index"],
                    "incar": item["incar"],
                    "kpoints": item["kpoints"],
                    "kpoints_text": render_kpoints(item["kpoints"]) if item["kpoints"] else None,
                }
                for item in inputs
            ],
        },
        "unavailable_from_v1_machine_interface": list(UNAVAILABLE_PLAN),
    }
