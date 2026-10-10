"""Output for ``bmd-run api ...``: the versioned JSON envelope and human-readable text.

Schema ``bmd_run.machine_output`` version 1
(``schemas/machine-output-v1.schema.json``). It is separate from the read/build
schema ``bmd_run.output`` v3, which is unchanged.

Every renderer works only from client-owned result fields produced by
``machine.py``. Compute messages and suggestions are never shown; errors carry
client text plus the client's copy of the recognised Compute error code.
"""

from __future__ import annotations

import json
from typing import List, Optional

from . import CLIENT_NAME, __version__
from . import api_vocabulary as vocab
from .bounded import render_number
from .errors import ApiError, ClientError
from .machine import render_incar_value

OUTPUT_SCHEMA = "bmd_run.machine_output"
OUTPUT_SCHEMA_VERSION = 1
REFERENCE_COMMIT = "e3fbb3beaf5c0084023df9fcdd76deea4edc4778"
COMMANDS = ("plan", "prepare", "submit", "status")


def error_dict(error: ClientError) -> dict:
    document = error.to_dict()
    document["compute_code"] = getattr(error, "compute_code", None)
    details = dict(getattr(error, "details", {}) or {}) if isinstance(error, ApiError) else {}
    document["attempt_id"] = details.pop("attempt_id", None)
    document["fields"] = details.pop("fields", None)
    document["diagnostic_code"] = details.pop("diagnostic_code", None)
    document["resolved_plan_digest"] = details.pop("resolved_plan_digest", None)
    document["attempt"] = details.pop("attempt", None)
    return document


def envelope(*, command: Optional[str], api_url: Optional[str], request: Optional[dict], result: Optional[dict],
             error: Optional[ClientError], warnings: Optional[List[str]] = None) -> dict:
    return {
        "schema": OUTPUT_SCHEMA,
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "client": {"name": CLIENT_NAME, "version": __version__},
        "command": command,
        "ok": error is None,
        "compute_api": {"origin": api_url, "api_version": vocab.API_VERSION, "reference_commit": REFERENCE_COMMIT},
        "request": request,
        "result": result,
        "error": error_dict(error) if error is not None else None,
        "warnings": list(warnings or []),
    }


def to_json(document: dict) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


# --- text ---------------------------------------------------------------------------


def _show(value) -> str:
    return "(not recognised)" if value is None else str(value)


def _stage_line(stage: dict) -> str:
    modifiers = ", ".join(stage["modifiers"]) if stage["modifiers"] else "none"
    extra = f" (+{stage['unrecognized_modifier_count']} unrecognised)" if stage["unrecognized_modifier_count"] else ""
    line = (f"  {stage['index']}. {_show(stage['stage_type'])} / {_show(stage['theory'])}"
            f"  modifiers: {modifiers}{extra}  [{_show(stage['vasp_executable'])}]")
    if stage["dft_u"]:
        values = ", ".join(
            f"{element} U={render_number(v['U'])} J={render_number(v['J'])} L={render_number(v['L'])}"
            for element, v in stage["dft_u"]["species"].items()
        )
        line += f"\n       DFT+U (frozen by BMD Compute): {values}"
    return line


def _resources(resources: dict) -> List[str]:
    return [
        f"  Partition:  {_show(resources['partition'])}",
        f"  Account:    {_show(resources['account'])}",
        f"  Nodes:      {resources['nodes']}",
        f"  CPUs:       {resources['cpus']}",
        f"  Memory:     {resources['memory_gb']} GB",
        f"  Walltime:   {resources['walltime']}",
    ]


def render_plan(plan: dict) -> str:
    structure, workflow = plan["structure"], plan["workflow"]
    lines = [
        f"PLAN DIGEST  {plan['plan_digest']}  (version {plan['plan_digest_version']})",
        "",
        "STRUCTURE (resolved by BMD Compute)",
        f"  Formula:          {structure['formula']}  (reduced {structure['reduced_formula']})",
        f"  Atoms:            {structure['natoms']}",
        f"  Volume:           {render_number(structure['volume_angstrom3'])} Å³",
    ]
    if structure["space_group_number"] is not None:
        lines.append(f"  Space group:      #{structure['space_group_number']}")
    if structure["crystal_system"] is not None:
        lines.append(f"  Crystal system:   {structure['crystal_system']}")
    lines.append(f"  Canonical sha256: {structure['canonical_sha256']}")
    heading = workflow["desired_output"] or "custom"
    lines += ["", f"WORKFLOW (BMD Compute: {heading}, {workflow['stage_count']} stage{'s' if workflow['stage_count'] != 1 else ''})"]
    lines += [_stage_line(stage) for stage in workflow["stages"]]
    considerations = plan["method_considerations"]
    lines += ["", "METHOD CONSIDERATIONS (BMD Compute)"]
    if not considerations or not considerations["considerations"]:
        lines.append("  (none reported)")
    else:
        for item in considerations["considerations"]:
            stages = ", ".join(str(i) for i in item["automatic_stage_indices"]) or "none"
            lines.append(
                f"  - {item['id']}: {_show(item['automatic_application_state'])} "
                f"(stages {stages}; trigger elements {', '.join(item['trigger_elements']) or 'none'})"
            )
        if considerations["unrecognized_count"]:
            lines.append(f"  ({considerations['unrecognized_count']} unrecognised consideration(s) not shown)")
    lines += ["", "RESOURCES (resolved by BMD Compute)"] + _resources(plan["resources"])
    inputs = plan["scientific_inputs"]
    lines += ["", f"GENERATED INPUTS (POTCAR functional {_show(inputs['potcar_functional'])})"]
    for stage in inputs["stages"]:
        lines += ["", f"  Stage {stage['index']}  POTCAR: {' '.join(stage['potcar_symbols']) or '(none recognised)'}",
                  f"  INCAR (sha256 {stage['incar']['sha256']})"]
        lines += [f"    {item['tag']} = {render_incar_value(item['value'])}" for item in stage["incar"]["tags"]]
        if stage["incar"]["withheld_tag_count"]:
            lines.append(f"    ({stage['incar']['withheld_tag_count']} tag(s) withheld)")
        kpoints = stage["kpoints"]
        if kpoints is not None:
            if kpoints["mesh"] is not None:
                shift = " ".join(render_number(x) for x in kpoints["shift"]) if kpoints["shift"] else "0 0 0"
                text = f"{_show(kpoints['style'])} mesh {' '.join(str(n) for n in kpoints['mesh'])} shift {shift}"
            else:
                text = f"{_show(kpoints['style'])}, {kpoints['num_kpts']} explicit k-points (not shown)"
            lines.append(f"  KPOINTS: {text} (sha256 {kpoints['sha256']})")
    packages = plan["software"]["runtime_parity_packages"]
    lines += ["", "SOFTWARE (parity-critical packages)",
              "  " + ", ".join(f"{name} {_show(version)}" for name, version in packages.items())]
    return "\n".join(lines)


def render_attempt(attempt: dict) -> List[str]:
    submission, scheduler = attempt["submission"], attempt["scheduler"]
    lines = [
        f"ATTEMPT {attempt['attempt_id']}",
        f"  State (BMD Compute):  {attempt['state']}",
        f"  Plan digest:          {attempt['plan_digest']}",
        f"  Created:              {attempt['created_at']}",
        f"  Job ID:               {submission['job_id'] or '(none)'}",
    ]
    if scheduler is not None:
        lines.append(
            f"  Scheduler:            {scheduler['summary']} ({scheduler['state']})"
            + (f", elapsed {scheduler['elapsed']}" if scheduler["elapsed"] else "")
            + (f", exit {scheduler['exit_code']}" if scheduler["exit_code"] else "")
        )
    lines += ["  Resources:"] + ["  " + line for line in _resources(attempt["resources"])]
    return lines


def render_result(command: str, result: dict) -> str:
    if command == "plan":
        return render_plan(result)
    lines: List[str] = []
    if command == "prepare" and result.get("plan"):
        lines += [render_plan(result["plan"]), ""]
    lines += render_attempt(result["attempt"])
    local = result.get("local")
    if local is not None:
        lines.append(f"  Local record state:   {local['local_state']}")
    if command == "prepare":
        lines += ["", f"Prepared only. To submit this exact attempt: bmd-run api submit {result['attempt']['attempt_id']}"]
    if command == "submit" and result["attempt"]["state"] == "submitted":
        lines += ["", f"Check it with: bmd-run api status {result['attempt']['attempt_id']}"]
    return "\n".join(lines)


def render_error(error: ClientError) -> str:
    document = error_dict(error)
    lines = [f"error ({error.kind}): {error.message}"]
    if document["compute_code"]:
        lines.append(f"  BMD Compute code: {document['compute_code']}")
    if document["diagnostic_code"]:
        lines.append(f"  diagnostic: {document['diagnostic_code']}")
    for item in document["fields"] or []:
        lines.append(f"  field {item['field'] or '(unrecognised)'}: {item['problem'] or '(unrecognised problem)'}")
    if document["resolved_plan_digest"]:
        lines.append(f"  plan digest now resolved by BMD Compute: {document['resolved_plan_digest']}")
    if document["attempt_id"]:
        lines.append(f"  attempt: {document['attempt_id']}")
    if document["attempt"]:
        lines.append(f"  attempt state (BMD Compute): {document['attempt']['state']}")
    if error.suggestion:
        lines.append(f"  suggestion: {error.suggestion}")
    return "\n".join(lines)
