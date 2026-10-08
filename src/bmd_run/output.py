"""Versioned client-output envelope (``--json``) and human-readable rendering."""

from __future__ import annotations

import json
from typing import List, Optional

from . import CLIENT_NAME, __version__, probes
from .bounded import render_number
from .errors import ClientError

OUTPUT_SCHEMA = "bmd_run.output"
OUTPUT_SCHEMA_VERSION = 3
ADAPTER = "compute_v1_html"
ADAPTER_STATUS = "transitional"
COMMANDS = ("identity", "options", "analyze", "plan")


def envelope(
    *,
    command: Optional[str],
    base_url: Optional[str],
    request: Optional[dict],
    result: Optional[dict],
    error: Optional[ClientError],
    warnings: Optional[List[str]] = None,
) -> dict:
    return {
        "schema": OUTPUT_SCHEMA,
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "client": {"name": CLIENT_NAME, "version": __version__},
        "command": command,
        "ok": error is None,
        "compute": {
            "base_url": base_url,
            "adapter": ADAPTER,
            "adapter_status": ADAPTER_STATUS,
            "reference_release": probes.REFERENCE_RELEASE,
            "reference_commit": probes.REFERENCE_COMMIT,
        },
        "request": request,
        "result": result,
        "error": error.to_dict() if error is not None else None,
        "warnings": list(warnings or []),
    }


def to_json(document: dict) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


# --- text rendering ---------------------------------------------------------------
# Every renderer works only from Client-owned result fields.


def _indent(text: Optional[str], prefix: str = "    ") -> str:
    if not text:
        return prefix + "(none)"
    return "\n".join(prefix + line for line in text.rstrip("\n").split("\n"))


_STATUS_TEXT = {
    "applied": "applied automatically by BMD Compute",
    "not_applied_automatically": "not applied automatically",
    "advisory": "advisory (not applied)",
    "unrecognized": "status not recognised",
}


def _considerations(items: List[dict]) -> List[str]:
    if not items:
        return ["  (none reported)"]
    return [f"  - {item['label'] or 'Unrecognised consideration'}: {_STATUS_TEXT[item['status']]}" for item in items]


def _structure(structure: dict) -> List[str]:
    lattice = structure["lattice"]
    lines = [
        f"  Formula:          {structure['formula']}  (reduced {structure['reduced_formula']})",
        f"  Atoms:            {structure['natoms']}",
        f"  Volume:           {render_number(structure['volume_angstrom3'])} Å³",
        f"  Density:          {render_number(structure['density_g_cm3'])} g/cm³",
        "  Lattice a, b, c:  " + ", ".join(render_number(lattice[k]) for k in ("a", "b", "c")) + " Å",
        "  Angles:           " + ", ".join(render_number(lattice[k]) for k in ("alpha", "beta", "gamma")) + " degrees",
    ]
    if structure["space_group_number"] is not None:
        lines.append(f"  Space group:      #{structure['space_group_number']}")
    if structure["crystal_system"] is not None:
        lines.append(f"  Crystal system:   {structure['crystal_system']}")
    return lines


def _stage_lines(stages: List[dict], with_executable: bool) -> List[str]:
    lines = []
    for stage in stages:
        exe = f"  [{stage['vasp_executable']}]" if with_executable and stage.get("vasp_executable") else ""
        lines.append(f"  {stage['index']}. {stage['label']}{exe}")
    return lines


def render_identity(result: dict) -> str:
    lines = [
        "*** BEHAVIOURAL FINGERPRINT - NOT VERIFIED SOURCE IDENTITY ***",
        "",
        result["statement"],
        "",
        f"Reference: BMD Compute {result['reference']['release']} ({result['reference']['commit']})",
        f"Overall:   {'behaves like the reference' if result['matches_reference'] else 'DIFFERS from the reference'}",
        "",
    ]
    for name, probe in result["probes"].items():
        mark = "match" if probe["matches_reference"] else "DIFFERENT"
        lines.append(f"  {name:<20} {mark:<10} {probe['observed_sha256']}")
    return "\n".join(lines)


def render_options(result: dict) -> str:
    catalogue = result["catalogue"]
    lines = ["Desired Outputs offered by BMD Compute:"]
    for item in catalogue["desired_outputs"]:
        lines.append(f"  {item['value']:<28} {item['label']}")
        stages = " -> ".join(stage["label"] for stage in item["default_stages"])
        if stages:
            lines.append(f"  {'':<28} {stages}")
    for key, heading in (("stage_types", "Stage types"), ("theories", "Theories"), ("modifiers", "Modifiers")):
        values = [entry["value"] for entry in catalogue[key]]
        lines.append("")
        lines.append(f"{heading}: {', '.join(values) if values else '(none)'}")
    if catalogue["unrecognized_entry_count"]:
        lines.append(f"({catalogue['unrecognized_entry_count']} catalogue entries not in the v1.0.0 vocabulary were not shown)")
    lines.append("")
    lines.append(f"Catalogue sha256: {result['catalogue_sha256']}")
    return "\n".join(lines)


def render_analyze(result: dict) -> str:
    workflow = result["default_workflow"]
    lines = ["STRUCTURE (parsed from BMD Compute)"]
    lines.extend(_structure(result["structure"]))
    lines += ["", "METHOD CONSIDERATIONS (BMD Compute)"]
    lines.extend(_considerations(result["method_considerations"]))
    lines += ["", f"DEFAULT WORKFLOW ON ANALYZE: {workflow['desired_output']}"]
    lines.extend(_stage_lines(workflow["stages"], with_executable=False))
    return "\n".join(lines)


def render_plan(result: dict) -> str:
    workflow = result["resolved_workflow"]
    resources = result["resources"]
    count = workflow["stage_count"]

    def show(value, unit=""):
        return "(not recognised)" if value is None else f"{value}{unit}"

    lines = [f"RESOLVED WORKFLOW  (BMD Compute Desired Output: {workflow['desired_output']}, "
             f"{count} stage{'s' if count != 1 else ''})"]
    lines.extend(_stage_lines(workflow["stages"], with_executable=True))
    lines += [
        "",
        "RESOURCES",
        f"  Partition:  {show(resources['partition'])}",
        f"  Account:    {show(resources['account'])}",
        f"  CPUs:       {resources['cpus']}",
        f"  Nodes:      {resources['nodes']}",
        f"  Memory:     {resources['memory_gb']} GB",
        f"  Walltime:   {resources['walltime']}",
        "",
        result["status"],
        "",
        "METHOD CONSIDERATIONS / AUTOMATIC TREATMENTS (BMD Compute)",
    ]
    lines.extend(_considerations(result["method_considerations"]))
    lines += ["", "STRUCTURE (parsed from BMD Compute)"]
    lines.extend(_structure(result["structure"]))
    lines += ["", "GENERATED INPUTS (all stages)", f"  {result['generated_inputs']['note']}"]
    labels = {stage["index"]: stage["label"] for stage in workflow["stages"]}
    for stage in result["generated_inputs"]["stages"]:
        lines += ["", f"  Stage {stage['index']} - {labels.get(stage['index'], '')}", "  INCAR"]
        lines.append(_indent(stage["incar"]["text"]))
        if stage["incar"]["withheld_tag_count"]:
            lines.append(f"    ({stage['incar']['withheld_tag_count']} tag(s) withheld)")
        lines.append("  KPOINTS")
        lines.append(_indent(stage["kpoints_text"]))
    return "\n".join(lines)


RENDERERS = {
    "identity": render_identity,
    "options": render_options,
    "analyze": render_analyze,
    "plan": render_plan,
}


def render_error(error: ClientError) -> str:
    lines = [f"error ({error.kind}): {error.message}"]
    if error.compute_stage:
        lines.append(f"  BMD Compute stage: {error.compute_stage}")
    if error.suggestion:
        lines.append(f"  suggestion: {error.suggestion}")
    return "\n".join(lines)
