"""Machine-API operations: plan, prepare, submit and attempt status.

BMD Compute remains the only authority for methodology, workflow construction,
resource validation, remote preparation and SLURM submission. This module:

* builds requests that contain only what the user supplied (structure text
  read unchanged from the file, a Desired Output identifier or a Custom
  workflow in Compute's stage schema, and optional resources); it fills in no
  defaults and infers nothing;
* binds an attempt to a plan before any side effect: obtain the plan, record
  its digest, choose an attempt UUID, persist the request and identity, and
  only then send Prepare with ``submit=false``;
* resumes only from the persisted record, never from CLI arguments; Submit
  re-sends the persisted request with only ``submit`` changed to true;
* never creates another attempt after a timeout or an uncertain submission;
* projects Compute's JSON into client-owned output: vocabulary members are
  replaced by the client's own copies, numbers are bounded and re-rendered, and
  no Compute prose (messages, suggestions, option text, module names, symbols)
  is relayed. Unrecognised values are counted, not shown.
"""

from __future__ import annotations

import math
import re
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import api_vocabulary as vocab
from . import bounded
from .api_endpoints import ApiEndpoint, canonical_attempt_id
from .api_transport import ApiResponse, ApiTransport, RequestOutcomeUnknown
from .attempt_store import AttemptStore, add_event, new_record, utc_now
from .errors import (
    ApiError,
    AttemptConflict,
    AttemptNotFound,
    AuthenticationFailed,
    ClientError,
    ComputeRejected,
    InsufficientScope,
    InvalidRequest,
    NetworkTimeout,
    PlanDigestMismatch,
    QuotaExceeded,
    RemoteOperationFailed,
    ServiceUnavailable,
    SubmissionUncertain,
    UnexpectedResponse,
    UsageError,
)
from .incar_tags import TAGS

MAX_CUSTOM_STAGES = 16
MAX_MODIFIERS = 8
MAX_OPTIONS_BYTES = 64 * 1024

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
_WALLTIME = re.compile(r"^[0-9]{1,3}:[0-5][0-9]:[0-5][0-9]$")
_QUEUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PLAN_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UTC = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z$")
_LOCAL_TIME = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})[ T]([0-9]{2}):([0-9]{2}):([0-9]{2})$")
_JOB_ID = re.compile(r"^([0-9]{1,20})(?:_([0-9]{1,10}))?$")
_EXIT_CODE = re.compile(r"^([0-9]{1,3}):([0-9]{1,3})$")
_ELAPSED = re.compile(r"^(?:([0-9]{1,4})-)?([0-9]{1,2}):([0-9]{2}):([0-9]{2})$")
_VERSION = re.compile(r"^([0-9]{1,6})(?:\.([0-9]{1,6}))?(?:\.([0-9]{1,6}))?(?:\.([0-9]{1,6}))?$")
_FIELD_SEGMENT = re.compile(r"\.?([a-z_]{1,32})|\[([0-9]{1,2})\]")
# Request field names a Compute ``invalid_request`` error may name (the client's own request vocabulary).
REQUEST_FIELD_NAMES = (
    "structure", "format", "text", "workflow", "desired_output", "custom", "stages", "stage_type",
    "theory", "modifiers", "options", "resources", "cpus", "memory_gb", "walltime", "queue",
    "expected_plan_digest", "submit", "labels", "campaign", "cell",
)
_POTCAR = re.compile(r"^([A-Z][a-z]?)(?:_([A-Za-z0-9_]{1,8}))?$")

COMMAND_SCOPES = {"plan": "plan", "prepare": "prepare", "submit": "submit", "status": "read"}

# Fields of the Compute plan response that the client never relays.
UNAVAILABLE_FROM_PLAN = (
    "structure.space_group_symbol",
    "workflow.stages.options (frozen values are covered by plan_digest; DFT+U values are shown)",
    "automatic_treatments (summarised by method_considerations)",
    "method_considerations prose and policy sources",
    "software.modules (runtime environment)",
    "policies identifiers",
    "error messages and suggestions from BMD Compute",
)


# =============================================================== request building


def plan_request(*, structure_text: str, structure_format: str, desired_output: Optional[str],
                 custom_workflow: Optional[dict], resources: Mapping[str, Any]) -> dict:
    """Assemble a plan request from user input. Shape checks only; Compute decides validity."""

    if structure_format not in vocab.STRUCTURE_FORMATS:
        raise UsageError("The structure format must be poscar or cif.")
    if not isinstance(structure_text, str) or not structure_text.strip():
        raise UsageError("The structure file is empty.")
    if (desired_output is None) == (custom_workflow is None):
        raise UsageError("Give exactly one of --desired-output or --custom-workflow.")
    if desired_output is not None:
        if not _IDENTIFIER.match(desired_output):
            raise UsageError("The Desired Output must be an identifier such as energy_only.")
        workflow: dict = {"desired_output": desired_output}
    else:
        workflow = {"custom": custom_workflow_shape(custom_workflow)}
    request = {"structure": {"format": structure_format, "text": structure_text}, "workflow": workflow}
    if resources:
        request["resources"] = dict(resources)
    return request


def custom_workflow_shape(document: Any) -> dict:
    """Check that a Custom workflow uses Compute's stage schema exactly; values are not judged."""

    if not isinstance(document, dict) or set(document) != {"stages"}:
        raise UsageError('A Custom workflow must be a JSON object {"stages": [...]} and nothing else.')
    stages = document["stages"]
    if not isinstance(stages, list) or not 1 <= len(stages) <= MAX_CUSTOM_STAGES:
        raise UsageError(f"A Custom workflow needs 1 to {MAX_CUSTOM_STAGES} stages.")
    shaped = []
    for number, stage in enumerate(stages, start=1):
        if not isinstance(stage, dict) or not {"stage_type", "theory"} <= set(stage) <= {"stage_type", "theory", "modifiers", "options"}:
            raise UsageError(
                f"Custom workflow stage {number} must have stage_type and theory, and may have only "
                "modifiers and options besides."
            )
        for key in ("stage_type", "theory"):
            if not isinstance(stage[key], str) or not _IDENTIFIER.match(stage[key]):
                raise UsageError(f"Custom workflow stage {number}: {key} must be an identifier.")
        modifiers = stage.get("modifiers", [])
        if not isinstance(modifiers, list) or len(modifiers) > MAX_MODIFIERS or not all(
            isinstance(item, str) and _IDENTIFIER.match(item) for item in modifiers
        ):
            raise UsageError(f"Custom workflow stage {number}: modifiers must be a list of identifiers.")
        options = stage.get("options", {})
        if not isinstance(options, dict):
            raise UsageError(f"Custom workflow stage {number}: options must be an object.")
        shaped.append({"stage_type": stage["stage_type"], "theory": stage["theory"],
                       "modifiers": list(modifiers), "options": options})
    return {"stages": shaped}


def resources_request(*, cpus: Optional[str], memory_gb: Optional[str], walltime: Optional[str],
                      queue: Optional[str]) -> dict:
    resources: dict = {}
    for name, value in (("cpus", cpus), ("memory_gb", memory_gb)):
        if value is None:
            continue
        if not re.match(r"^[0-9]{1,6}$", value) or int(value) <= 0:
            raise UsageError(f"--{name.replace('_', '-')} must be a positive integer.")
        resources[name] = int(value)
    if walltime is not None:
        if not _WALLTIME.match(walltime):
            raise UsageError("--walltime must be HH:MM:SS.")
        resources["walltime"] = walltime
    if queue is not None:
        if not _QUEUE.match(queue):
            raise UsageError("--queue must be a short identifier.")
        resources["queue"] = queue
    return resources


def attempt_body(record: Mapping[str, Any], *, submit: bool) -> dict:
    """The PUT body, built only from the persisted record."""

    body = dict(record["request"])
    body["expected_plan_digest"] = record["expected_plan_digest"]
    body["submit"] = bool(submit)
    return body


def request_summary(request: Mapping[str, Any]) -> dict:
    """Client-owned summary of a request (no structure text)."""

    workflow = request["workflow"]
    custom = workflow.get("custom")
    return {
        "structure_format": request["structure"]["format"],
        "workflow_mode": "custom" if custom is not None else "desired_output",
        "desired_output": workflow.get("desired_output"),
        "custom_stage_count": len(custom["stages"]) if custom is not None else None,
        "resources": dict(request.get("resources") or {}),
    }


# ================================================================== projection

_FAIL = bounded.fail


def _choice(value, options, field):
    return bounded.choice(value, options, field)


def _member_or_none(value, options):
    return bounded.optional_choice(value, options)


def _int(value, field, low=0, high=10**9):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _FAIL(field)
    return int(value)


def _number(value, field, low=-1e12, high=1e12):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _FAIL(field)
    number = float(value) if isinstance(value, float) else value
    if isinstance(number, float) and (not math.isfinite(number)):
        raise _FAIL(field)
    if not low <= number <= high:
        raise _FAIL(field)
    if isinstance(number, float):
        number = float(f"{number:.12g}")
    return number


def _match(value, pattern, field):
    if not isinstance(value, str) or not pattern.match(value):
        raise _FAIL(field)
    return pattern.match(value)


def _digest(value, field) -> str:
    _match(value, _PLAN_DIGEST, field)
    return "sha256:" + value[7:]


def _hex(value, field) -> str:
    _match(value, _SHA256, field)
    return value


def _utc(value, field) -> str:
    m = _match(value, _UTC, field)
    return "%s-%s-%sT%s:%s:%sZ" % m.groups()


def _optional(value, convert, field):
    return None if value is None else convert(value, field)


def _local_time(value, field) -> str:
    m = _match(value, _LOCAL_TIME, field)
    return "%s-%s-%s %s:%s:%s" % m.groups()


def _job_id(value, field) -> str:
    m = _match(value, _JOB_ID, field)
    return str(int(m.group(1))) + (f"_{int(m.group(2))}" if m.group(2) is not None else "")


def _walltime(value, field) -> str:
    _match(value, _WALLTIME, field)
    hours, minutes, seconds = value.split(":")
    return f"{int(hours):02d}:{minutes}:{seconds}"


def _object(value, field) -> dict:
    if not isinstance(value, dict):
        raise _FAIL(field)
    return value


def _list(value, field, limit=4096) -> list:
    if not isinstance(value, list) or len(value) > limit:
        raise _FAIL(field)
    return value


def project_resources(value, field="resources") -> dict:
    resources = _object(value, field)
    return {
        "nodes": _int(resources.get("nodes"), f"{field}.nodes", 1, 10**4),
        "cpus": _int(resources.get("cpus"), f"{field}.cpus", 1, 10**6),
        "memory_gb": _int(resources.get("memory_gb"), f"{field}.memory_gb", 1, 10**6),
        "walltime": _walltime(resources.get("walltime"), f"{field}.walltime"),
        "partition": _member_or_none(resources.get("partition"), vocab.PARTITIONS),
        "account": _member_or_none(resources.get("account"), vocab.ACCOUNTS),
    }


def _modifiers(value, field) -> Tuple[List[str], int]:
    items = _list(value, field, 32)
    known = [m for m in (bounded.optional_choice(item, vocab.MODIFIERS) for item in items) if m is not None]
    return known, len(items) - len(known)


def _dft_u(option, field) -> Optional[dict]:
    """The frozen DFT+U parameters: element symbols and numbers only."""

    if not isinstance(option, dict):
        return None
    species = option.get("species")
    if not isinstance(species, dict) or len(species) > 120:
        raise _FAIL(f"{field}.species")
    projected = {}
    for element, values in sorted(species.items()):
        symbol = _choice(element, vocab.ELEMENTS, f"{field}.species")
        values = _object(values, f"{field}.species")
        projected[symbol] = {
            key: _number(values.get(key), f"{field}.species.{key}", -100, 100) for key in ("U", "J", "L")
        }
    ldautype = option.get("LDAUTYPE")
    return {
        "ldautype": None if ldautype is None else _int(ldautype, f"{field}.LDAUTYPE", 1, 4),
        "species": projected,
    }


def _stage(value, index, field) -> dict:
    stage = _object(value, field)
    if _int(stage.get("index"), f"{field}.index", 1, 64) != index:
        raise _FAIL(f"{field}.index")
    modifiers, unknown_modifiers = _modifiers(stage.get("modifiers"), f"{field}.modifiers")
    options = _object(stage.get("options"), f"{field}.options")
    return {
        "index": index,
        "stage_type": _member_or_none(stage.get("stage_type"), vocab.STAGE_TYPES),
        "theory": _member_or_none(stage.get("theory"), vocab.THEORIES),
        "modifiers": modifiers,
        "unrecognized_modifier_count": unknown_modifiers,
        "vasp_executable": _member_or_none(stage.get("vasp_executable"), vocab.VASP_EXECUTABLES),
        "option_count": len(options),
        "dft_u": _dft_u(options.get("dft_u"), f"{field}.options.dft_u") if "dft_u" in options else None,
    }


def _considerations(value, field) -> Optional[dict]:
    if value is None:
        return None
    context = _object(value, field)
    items = _list(context.get("considerations"), f"{field}.considerations", 64)
    projected, unrecognized = [], 0
    for position, item in enumerate(items):
        item_field = f"{field}.considerations[{position}]"
        item = _object(item, item_field)
        identifier = _member_or_none(item.get("id"), vocab.CONSIDERATION_IDS)
        if identifier is None:
            unrecognized += 1
            continue
        stage_indices = [
            _int(index, f"{item_field}.automatic_stage_indices", 1, 64)
            for index in _list(item.get("automatic_stage_indices") or [], f"{item_field}.automatic_stage_indices", 64)
        ]
        projected.append({
            "id": identifier,
            "method": _member_or_none(item.get("method"), vocab.CONSIDERATION_METHODS),
            "modifier": _member_or_none(item.get("modifier"), vocab.MODIFIERS),
            "status": _member_or_none(item.get("status"), vocab.CONSIDERATION_STATUSES),
            "selection_state": _member_or_none(item.get("selection_state"), vocab.SELECTION_STATES),
            "automatic_application_state": _member_or_none(
                item.get("automatic_application_state"), vocab.AUTOMATIC_APPLICATION_STATES
            ),
            "trigger_elements": [
                symbol for symbol in (
                    bounded.optional_choice(e, vocab.ELEMENTS)
                    for e in _list(item.get("trigger_elements") or [], f"{item_field}.trigger_elements", 120)
                ) if symbol is not None
            ],
            "automatic_stage_indices": stage_indices,
        })
    policy_version = context.get("policy_version")
    return {
        "policy_version": None if policy_version is None else _int(policy_version, f"{field}.policy_version", 0, 10**6),
        "considerations": projected,
        "unrecognized_count": unrecognized,
    }


def _incar(settings, field) -> dict:
    settings = _object(settings, field)
    if len(settings) > 400:
        raise _FAIL(field)
    tags, withheld = [], 0
    for tag in sorted(settings, key=str):
        spec = TAGS.get(tag) if isinstance(tag, str) else None
        value = settings[tag]
        if spec is None or spec[0] == "opaque":
            withheld += 1
            continue
        kind, words = spec
        projected = _incar_value(value, kind, words, f"{field}.value")
        if projected is None:
            withheld += 1
            continue
        tags.append({"tag": [t for t in TAGS if t == tag][0], "value": projected})
    return {"tags": tags, "withheld_tag_count": withheld}


def _incar_scalar(value, field):
    if isinstance(value, bool):
        return value
    return _number(value, field)


def _incar_value(value, kind, words, field):
    if kind == "list":
        items = _list(value, field) if isinstance(value, list) else [value]
        return [_incar_scalar(item, field) for item in items]
    if isinstance(value, list):
        return None
    if kind == "bool":
        if not isinstance(value, bool):
            raise _FAIL(field)
        return value
    if kind in ("int", "float"):
        return _number(value, field)
    if isinstance(value, bool) and kind == "bool_or_word":
        return value
    if isinstance(value, str):
        return _word(value, words)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _number(value, field)
    return None


def _word(value: str, words) -> Optional[str]:
    """Case-insensitive member of a tag's closed word list, in the client's spelling."""

    folded = value.casefold()
    for word in words:
        if word.casefold() == folded:
            return word
    return None


def render_incar_value(value) -> str:
    if isinstance(value, list):
        return " ".join(bounded.render_number(item) for item in value)
    if isinstance(value, str):
        return value
    return bounded.render_number(value)


def _kpoints(value, field) -> Optional[dict]:
    if value is None:
        return None
    kpoints = _object(value, field)
    count = _int(kpoints.get("num_kpts"), f"{field}.num_kpts", 0, 10**6)
    result = {
        "style": _member_or_none(kpoints.get("style"), vocab.KPOINT_STYLES),
        "num_kpts": count,
        "sha256": _hex(kpoints.get("sha256"), f"{field}.sha256"),
        "mesh": None,
        "shift": None,
    }
    if count == 0 and kpoints.get("kpts") is not None:
        rows = _list(kpoints.get("kpts"), f"{field}.kpts", 1)
        if len(rows) != 1:
            raise _FAIL(f"{field}.kpts")
        mesh = _list(rows[0], f"{field}.kpts", 3)
        result["mesh"] = [_int(n, f"{field}.kpts", 1, 1000) for n in mesh]
        if kpoints.get("kpts_shift") is not None:
            shift = _list(kpoints.get("kpts_shift"), f"{field}.kpts_shift", 3)
            result["shift"] = [_number(x, f"{field}.kpts_shift", -1, 1) for x in shift]
    return result


def _potcar_symbol(value, field) -> Optional[str]:
    if not isinstance(value, str):
        raise _FAIL(field)
    m = _POTCAR.match(value)
    if not m:
        return None
    element = bounded.optional_choice(m.group(1), vocab.ELEMENTS)
    if element is None:
        return None
    if m.group(2) is None:
        return element
    suffix = bounded.optional_choice(m.group(2), vocab.POTCAR_SUFFIXES)
    return None if suffix is None else f"{element}_{suffix}"


def _input_stage(value, index, field) -> dict:
    stage = _object(value, field)
    if _int(stage.get("index"), f"{field}.index", 1, 64) != index:
        raise _FAIL(f"{field}.index")
    incar = _object(stage.get("incar"), f"{field}.incar")
    poscar = _object(stage.get("poscar"), f"{field}.poscar")
    symbols = [_potcar_symbol(s, f"{field}.potcar_symbols") for s in _list(stage.get("potcar_symbols"), f"{field}.potcar_symbols", 120)]
    return {
        "index": index,
        "incar": {**_incar(incar.get("settings"), f"{field}.incar.settings"),
                  "sha256": _hex(incar.get("sha256"), f"{field}.incar.sha256")},
        "kpoints": _kpoints(stage.get("kpoints"), f"{field}.kpoints"),
        "potcar_symbols": [s for s in symbols if s is not None],
        "unrecognized_potcar_symbol_count": sum(1 for s in symbols if s is None),
        "poscar": {
            "num_sites": _int(poscar.get("num_sites"), f"{field}.poscar.num_sites", 1, 10**6),
            "sha256": _hex(poscar.get("sha256"), f"{field}.poscar.sha256"),
        },
    }


def _packages(value, field) -> dict:
    packages = _object(value, field)
    projected = {}
    for name in vocab.PARITY_PACKAGES:
        version = packages.get(name)
        if version is None:
            projected[name] = None
            continue
        m = _VERSION.match(version) if isinstance(version, str) else None
        projected[name] = ".".join(str(int(part)) for part in m.groups() if part is not None) if m else None
    return projected


def _policies(value, field) -> dict:
    policies = _object(value, field)
    projected = {}
    for name in vocab.POLICY_NAMES:
        entry = policies.get(name)
        version = entry.get("version") if isinstance(entry, dict) else None
        projected[name] = None if version is None or isinstance(version, bool) or not isinstance(version, int) else _int(version, f"{field}.{name}", 0, 10**6)
    return projected


def project_plan(document: Any, sent: Mapping[str, Any]) -> dict:
    """Validate a ``bmd_compute.api.plan`` v1 document and return the client's projection."""

    plan = _object(document, "plan")
    if plan.get("schema") != vocab.PLAN_SCHEMA or plan.get("schema_version") != vocab.RESPONSE_SCHEMA_VERSION:
        raise _FAIL("plan.schema")
    if plan.get("api_version") != vocab.API_VERSION:
        raise _FAIL("plan.api_version")
    request = _object(plan.get("request"), "plan.request")
    expected = request_summary(sent)
    if (
        request.get("structure_format") != expected["structure_format"]
        or request.get("workflow_mode") != expected["workflow_mode"]
        or request.get("desired_output") != expected["desired_output"]
    ):
        raise UnexpectedResponse("BMD Compute answered a plan for a different request than the one sent.")
    structure = _object(plan.get("structure"), "plan.structure")
    formula = bounded.render_formula(bounded.formula(structure.get("formula"), "plan.structure.formula"), reduced=False)
    reduced = bounded.render_formula(
        bounded.formula(structure.get("reduced_formula"), "plan.structure.reduced_formula"), reduced=True
    )
    workflow = _object(plan.get("workflow"), "plan.workflow")
    stage_values = _list(workflow.get("stages"), "plan.workflow.stages", 64)
    stage_count = _int(workflow.get("stage_count"), "plan.workflow.stage_count", 1, 64)
    if stage_count != len(stage_values):
        raise _FAIL("plan.workflow.stage_count")
    inputs = _object(plan.get("scientific_inputs"), "plan.scientific_inputs")
    input_stages = _list(inputs.get("stages"), "plan.scientific_inputs.stages", 64)
    if len(input_stages) != stage_count:
        raise _FAIL("plan.scientific_inputs.stages")
    software = _object(plan.get("software"), "plan.software")
    space_group = structure.get("space_group_number")
    crystal_system = structure.get("crystal_system")
    recipe = workflow.get("recipe")
    return {
        "plan_digest": _digest(plan.get("plan_digest"), "plan.plan_digest"),
        "plan_digest_version": _choice(plan.get("plan_digest_version"), vocab.PLAN_DIGEST_VERSIONS, "plan.plan_digest_version"),
        "request": expected,
        "structure": {
            "formula": formula,
            "reduced_formula": reduced,
            "natoms": _int(structure.get("natoms"), "plan.structure.natoms", 1, 10**6),
            "space_group_number": None if space_group is None else _int(space_group, "plan.structure.space_group_number", 1, 230),
            "crystal_system": None if crystal_system is None else _member_or_none(crystal_system, vocab.CRYSTAL_SYSTEMS),
            "volume_angstrom3": _number(structure.get("volume"), "plan.structure.volume", 0, 1e9),
            "canonical_sha256": _hex(structure.get("canonical_sha256"), "plan.structure.canonical_sha256"),
        },
        "workflow": {
            "desired_output": None if recipe is None else _member_or_none(recipe, vocab.DESIRED_OUTPUTS),
            "stage_count": stage_count,
            "stages": [_stage(stage, index, f"plan.workflow.stages[{index}]") for index, stage in enumerate(stage_values, start=1)],
        },
        "method_considerations": _considerations(plan.get("method_considerations"), "plan.method_considerations"),
        "resources": project_resources(plan.get("resources"), "plan.resources"),
        "scientific_inputs": {
            "potcar_functional": _member_or_none(inputs.get("potcar_functional"), vocab.POTCAR_FUNCTIONALS),
            "stages": [_input_stage(stage, index, f"plan.scientific_inputs.stages[{index}]") for index, stage in enumerate(input_stages, start=1)],
        },
        "software": {
            "module_count": len(_list(software.get("modules"), "plan.software.modules", 64)),
            "runtime_parity_packages": _packages(software.get("runtime_parity_packages"), "plan.software.runtime_parity_packages"),
        },
        "policies": _policies(plan.get("policies"), "plan.policies"),
        "capability_schema_version": _int(plan.get("capability_schema_version"), "plan.capability_schema_version", 0, 10**6),
        "unavailable_from_compute_api": list(UNAVAILABLE_FROM_PLAN),
    }


def _scheduler(value, field) -> Optional[dict]:
    if value is None:
        return None
    scheduler = _object(value, field)
    exit_code = scheduler.get("exit_code")
    elapsed = scheduler.get("elapsed")
    available = scheduler.get("available")
    terminal = scheduler.get("terminal")
    if not isinstance(available, bool) or not isinstance(terminal, bool):
        raise _FAIL(field)
    return {
        "available": available,
        "summary": _member_or_none(scheduler.get("summary"), vocab.SCHEDULER_SUMMARIES) or "UNKNOWN",
        "state": _member_or_none(scheduler.get("state"), vocab.SLURM_STATES) or "UNKNOWN",
        "exit_code": None if exit_code is None else "%d:%d" % tuple(int(g) for g in _match(exit_code, _EXIT_CODE, f"{field}.exit_code").groups()),
        "elapsed": None if elapsed is None else _elapsed(elapsed, f"{field}.elapsed"),
        "started_at": _optional(scheduler.get("started_at"), _local_time, f"{field}.started_at"),
        "ended_at": _optional(scheduler.get("ended_at"), _local_time, f"{field}.ended_at"),
        "terminal": terminal,
        "checked_at": _optional(scheduler.get("checked_at"), _utc, f"{field}.checked_at"),
    }


def _elapsed(value, field) -> str:
    m = _match(value, _ELAPSED, field)
    days, hours, minutes, seconds = m.groups()
    text = f"{int(hours):02d}:{minutes}:{seconds}"
    return f"{int(days)}-{text}" if days is not None else text


def project_attempt(document: Any, *, attempt_id: str, expected_plan_digest: Optional[str]) -> dict:
    """Validate a ``bmd_compute.api.attempt`` v1 document for the attempt that was asked about."""

    attempt = _object(document, "attempt")
    if attempt.get("schema") != vocab.ATTEMPT_SCHEMA or attempt.get("schema_version") != vocab.RESPONSE_SCHEMA_VERSION:
        raise _FAIL("attempt.schema")
    if attempt.get("api_version") != vocab.API_VERSION:
        raise _FAIL("attempt.api_version")
    if attempt.get("attempt_id") != attempt_id:
        raise UnexpectedResponse("BMD Compute answered about a different attempt than the one requested.")
    digest = _digest(attempt.get("plan_digest"), "attempt.plan_digest")
    if expected_plan_digest is not None and digest != expected_plan_digest:
        raise UnexpectedResponse("BMD Compute reports a different plan digest for this attempt than the one it is bound to.")
    labels = _object(attempt.get("labels"), "attempt.labels")
    if labels:
        raise _FAIL("attempt.labels")  # bmd-run never sends labels
    submission = _object(attempt.get("submission"), "attempt.submission")
    requested = submission.get("requested")
    if not isinstance(requested, bool):
        raise _FAIL("attempt.submission.requested")
    state = _choice(attempt.get("state"), vocab.ATTEMPT_STATES, "attempt.state")
    job_id = _optional(submission.get("job_id"), _job_id, "attempt.submission.job_id")
    if state == "submitted" and job_id is None:
        raise _FAIL("attempt.submission.job_id")
    return {
        "attempt_id": attempt_id,
        "plan_digest": digest,
        "state": state,
        "created_at": _utc(attempt.get("created_at"), "attempt.created_at"),
        "resources": project_resources(attempt.get("resources"), "attempt.resources"),
        "submission": {
            "requested": requested,
            "job_id": job_id,
            "submitted_at_local": _optional(submission.get("submitted_at_local"), _local_time, "attempt.submission.submitted_at_local"),
        },
        "scheduler": _scheduler(attempt.get("scheduler"), "attempt.scheduler"),
    }


# ============================================================ error classification

_CLASSIFICATION: Dict[str, Tuple[type, str, Optional[str]]] = {
    "unauthenticated": (AuthenticationFailed, "BMD Compute did not accept the API token.",
                        "Check that the token is current and enabled; ask the BMD Compute administrator if needed."),
    "insufficient_scope": (InsufficientScope, "The API token does not grant the '{scope}' scope this command needs.",
                           "Planning-only tokens cannot prepare or submit. Use a token issued with the needed scope."),
    "attempt_forbidden": (AttemptConflict, "This attempt belongs to a different API principal.",
                          "Use the token that created the attempt."),
    "loopback_required": (ComputeRejected, "BMD Compute only serves the machine API to loopback clients.",
                          "Reach it through the SSH tunnel's loopback end."),
    "query_not_allowed": (ComputeRejected, "BMD Compute refused the request shape (query string).", None),
    "invalid_json": (ComputeRejected, "BMD Compute could not read the request body.", None),
    "unsupported_media_type": (ComputeRejected, "BMD Compute refused the request content type.", None),
    "request_too_large": (ComputeRejected, "The request is larger than BMD Compute accepts.", "Use a smaller structure file."),
    "invalid_request": (InvalidRequest, "BMD Compute rejected the request fields (see error.fields).", None),
    "structure_invalid": (InvalidRequest, "BMD Compute could not read the structure.",
                          "Check that the file is a valid POSCAR or CIF and that --format matches it."),
    "calculation_invalid": (InvalidRequest, "BMD Compute rejected the requested calculation.",
                            "See error.diagnostic_code; check the Desired Output or Custom workflow."),
    "invalid_attempt_id": (InvalidRequest, "BMD Compute rejected the attempt ID.", None),
    "resource_limit_exceeded": (InvalidRequest, "The resources exceed the machine-API execution limits.",
                                "Request at most 96 CPUs, 128 GB and 72:00:00 on one node."),
    "plan_digest_mismatch": (PlanDigestMismatch, "BMD Compute now resolves this request to a different plan than the one the attempt is bound to.",
                             "Nothing was prepared. Review the new plan with 'bmd-run api plan' and prepare a new attempt."),
    "plan_changed": (PlanDigestMismatch, "The plan bound to this attempt no longer resolves identically on BMD Compute.",
                     "The attempt cannot continue. Review the new plan and prepare a new attempt deliberately."),
    "attempt_request_mismatch": (AttemptConflict, "This attempt ID is already bound to a different request on BMD Compute.", None),
    "attempt_fingerprint_mismatch": (AttemptConflict, "BMD Compute no longer rebuilds the identical submission for this attempt.", None),
    "runtime_package_changed": (AttemptConflict, "This attempt was prepared by a different BMD Compute runtime and will not be replaced.",
                                "Prepare a new attempt deliberately if you still want this calculation."),
    "attempt_in_progress": (AttemptConflict, "Another request is preparing or submitting this attempt.",
                            "Wait, then check it with 'bmd-run api status'."),
    "attempt_busy": (AttemptConflict, "BMD Compute could not register the attempt yet.", "Retry the same command."),
    "remote_state_invalid": (AttemptConflict, "The attempt's state on POWER is not in an expected state.",
                             "Do not retry automatically; ask the BMD Compute administrator."),
    "attempt_not_found": (AttemptNotFound, "BMD Compute has no machine attempt with this ID for you.", None),
    "submission_uncertain": (SubmissionUncertain, "The SLURM submission outcome for this attempt is uncertain.",
                             "It will never be resubmitted automatically. Check it later with 'bmd-run api status'."),
    "submission_outcome_unconfirmed": (SubmissionUncertain, "BMD Compute could not confirm the submission outcome.",
                                       "Check it with 'bmd-run api status' before doing anything else."),
    "active_job_cap_exceeded": (QuotaExceeded, "You already have the maximum number of active machine-API jobs.", "Wait for a job to finish."),
    "submission_cap_exceeded": (QuotaExceeded, "You have reached the machine-API submission limit for the last 24 hours.", None),
    "attempt_cap_exceeded": (QuotaExceeded, "You have reached the machine-API limit on new attempts for the last 24 hours.", None),
    "api_not_configured": (ServiceUnavailable, "The BMD Compute machine API is not configured on this service.", None),
    "execution_not_configured": (ServiceUnavailable, "Machine execution is not configured on this BMD Compute service.", None),
    "remote_busy": (ServiceUnavailable, "BMD Compute is busy with other remote operations.", "Retry the same command shortly."),
    "remote_unavailable": (ServiceUnavailable, "BMD Compute could not reach POWER.", "Retry the same command later."),
    "remote_state_unconfirmed": (ServiceUnavailable, "BMD Compute could not confirm the preparation on POWER.", "Retry the same command."),
    "submission_not_started": (ServiceUnavailable, "The submission did not start.", "Retry the same command or check it with 'bmd-run api status'."),
    "plan_failed": (ServiceUnavailable, "BMD Compute failed while generating the plan.", None),
    "attempt_failed": (ServiceUnavailable, "BMD Compute failed while processing the attempt.", "Check it with 'bmd-run api status'."),
    "lookup_failed": (ServiceUnavailable, "BMD Compute failed while reading the attempt.", None),
    "server_misconfigured": (ServiceUnavailable, "BMD Compute reported a configuration problem.", None),
    "prepare_failed": (RemoteOperationFailed, "Remote preparation failed on POWER.", "Retry the same command; nothing was submitted."),
    "submit_failed": (RemoteOperationFailed, "SLURM refused the submission; the attempt is still prepared.",
                      "Retry with the same attempt, or ask the BMD Compute administrator."),
}


def classify_error(response: ApiResponse, *, scope: str, attempt_id: Optional[str] = None,
                   expected_plan_digest: Optional[str] = None) -> ClientError:
    """Turn a non-200 machine-API response into a client error with client-owned text."""

    document = response.document
    error = document.get("error") if isinstance(document, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    statuses = vocab.ERROR_STATUS.get(code) if isinstance(code, str) else None
    if statuses is None or response.status not in statuses or document.get("api_version") != vocab.API_VERSION:
        return UnexpectedResponse(
            "BMD Compute answered with an error the client does not recognise.",
            http_status=response.status,
        )
    code = [known for known in vocab.ERROR_STATUS if known == code][0]
    cls, message, suggestion = _CLASSIFICATION[code]
    details: dict = {}
    if code == "invalid_request":
        details["fields"] = _fields(error.get("fields"))
    if code == "calculation_invalid":
        details["diagnostic_code"] = bounded.optional_choice(error.get("diagnostic_code"), vocab.DIAGNOSTIC_CODES)
    if code == "plan_digest_mismatch" and error.get("plan_digest") is not None:
        try:
            details["resolved_plan_digest"] = _digest(error.get("plan_digest"), "error.plan_digest")
        except UnexpectedResponse:
            details["resolved_plan_digest"] = None
    if code == "submission_uncertain" and attempt_id is not None and error.get("attempt") is not None:
        try:
            details["attempt"] = project_attempt(error.get("attempt"), attempt_id=attempt_id,
                                                 expected_plan_digest=expected_plan_digest)
        except UnexpectedResponse:
            details["attempt"] = None
    if issubclass(cls, ApiError):
        return cls(message.format(scope=scope), suggestion=suggestion, http_status=response.status,
                   compute_code=code, details=details)
    return cls(message, suggestion=suggestion, http_status=response.status)


def _fields(value) -> List[dict]:
    if not isinstance(value, list):
        return []
    fields = []
    for item in value[:32]:
        if not isinstance(item, dict):
            continue
        path = item.get("field")
        problem = bounded.optional_choice(item.get("problem"), vocab.FIELD_PROBLEMS)
        fields.append({"field": _field_path(path), "problem": problem})
    return fields


def _field_path(path) -> Optional[str]:
    """Rebuild a field path from the client's own field names, or None if any part is unknown."""

    if not isinstance(path, str) or not 0 < len(path) <= 128:
        return None
    parts, position = [], 0
    for match in _FIELD_SEGMENT.finditer(path):
        if match.start() != position:
            return None
        position = match.end()
        name, index = match.groups()
        if name is not None:
            known = bounded.optional_choice(name, REQUEST_FIELD_NAMES)
            if known is None or (parts and not match.group(0).startswith(".")) or (not parts and match.group(0).startswith(".")):
                return None
            parts.append(("." if parts else "") + known)
        else:
            parts.append(f"[{int(index)}]")
    return "".join(parts) if parts and position == len(path) else None


# ================================================================== operations

def _expect_200(response: ApiResponse, **context) -> Any:
    if response.status != 200:
        raise classify_error(response, **context)
    return response.document


def plan(api: ApiTransport, request: dict) -> dict:
    """``POST /api/v1/plans``: read-only planning; never touches an attempt."""

    try:
        response = api.call(ApiEndpoint.PLANS, body=request)
    except RequestOutcomeUnknown as unknown:
        raise _lost(unknown, "The plan request got no complete response.", "Planning has no side effects; retry.") from None
    return project_plan(_expect_200(response, scope="plan"), request)


def _lost(unknown: RequestOutcomeUnknown, message: str, suggestion: str) -> ClientError:
    cls = NetworkTimeout if unknown.timed_out else ServiceUnavailable
    return cls(message, suggestion=suggestion)


def _observe(record: dict, projection: dict) -> None:
    record["local_state"] = projection["state"]
    record["last_observed"] = {
        "state": projection["state"],
        "job_id": projection["submission"]["job_id"],
        "scheduler_summary": (projection["scheduler"] or {}).get("summary"),
        "observed_at": utc_now(),
    }
    add_event(record, "observed", projection["state"])


def _check_origin(record: dict, api: ApiTransport) -> None:
    if record["api_origin"] != api.base_url:
        raise AttemptConflict(
            "This attempt was created against a different BMD Compute API origin.",
            suggestion=f"Use --api-url {record['api_origin']} for this attempt.",
        )


def prepare_new(api: ApiTransport, store: AttemptStore, request: dict, structure_source: dict, *,
                expected_plan_digest: Optional[str] = None,
                new_attempt_id: Callable[[], str] = lambda: str(uuid.uuid4())) -> dict:
    """Plan, bind, persist, then prepare (``submit=false``)."""

    store.ensure()
    planned = plan(api, request)
    digest = planned["plan_digest"]
    if expected_plan_digest is not None and digest != expected_plan_digest:
        raise PlanDigestMismatch(
            "BMD Compute now resolves this request to a different plan than --expect-plan-digest.",
            suggestion="Nothing was recorded or prepared. Review the plan again with 'bmd-run api plan'.",
            details={"resolved_plan_digest": digest},
        )
    attempt_id = canonical_attempt_id(new_attempt_id())
    if attempt_id is None:
        raise UsageError("Internal error: the new attempt ID is not a canonical UUID.")
    record = new_record(attempt_id=attempt_id, api_origin=api.base_url, request=request,
                        structure_source=structure_source, expected_plan_digest=digest)
    path = store.create(record)  # persisted before any remote side effect
    result = _send_prepare(api, store, record)
    return {"plan": planned, **result, "record_path": str(path)}


def prepare_resume(api: ApiTransport, store: AttemptStore, attempt_id: str) -> dict:
    """Repeat Prepare for a recorded attempt, from the record only."""

    record = _load(store, attempt_id)
    _check_origin(record, api)
    if record["submit_requested"]:
        raise UsageError(
            "A submission was already requested for this attempt; it is not prepared again.",
            suggestion=f"Use 'bmd-run api status {attempt_id}' or 'bmd-run api submit {attempt_id}'.",
        )
    return _send_prepare(api, store, record)


def _send_prepare(api: ApiTransport, store: AttemptStore, record: dict) -> dict:
    attempt_id = record["attempt_id"]
    add_event(record, "prepare_sent")
    store.update(record)
    try:
        response = api.call(ApiEndpoint.PUT_ATTEMPT, attempt_id=attempt_id, body=attempt_body(record, submit=False))
    except RequestOutcomeUnknown as unknown:
        record["local_state"] = "prepare_unconfirmed" if record["local_state"] == "planned" else record["local_state"]
        add_event(record, "outcome_unknown", "timeout" if unknown.timed_out else "connection_lost")
        store.update(record)
        cls = NetworkTimeout if unknown.timed_out else ServiceUnavailable
        raise cls(
            "The prepare request got no complete response; BMD Compute may or may not have registered it.",
            suggestion=f"Repeat it safely with: bmd-run api prepare --attempt {attempt_id}",
            details={"attempt_id": attempt_id},
        ) from None
    except (ServiceUnavailable, NetworkTimeout) as error:
        add_event(record, "not_sent")
        store.update(record)
        error.details["attempt_id"] = attempt_id
        error.suggestion = f"Nothing was sent. Retry with: bmd-run api prepare --attempt {attempt_id}"
        raise
    if response.status != 200:
        error = classify_error(response, scope="prepare", attempt_id=attempt_id,
                               expected_plan_digest=record["expected_plan_digest"])
        add_event(record, "compute_error", getattr(error, "compute_code", None) or error.kind)
        store.update(record)
        if isinstance(error, ApiError):
            error.details["attempt_id"] = attempt_id
        raise error
    try:
        projection = project_attempt(response.document, attempt_id=attempt_id,
                                     expected_plan_digest=record["expected_plan_digest"])
    except UnexpectedResponse as error:
        # Compute answered 200 but the answer is unusable: the outcome is not known.
        if record["local_state"] == "planned":
            record["local_state"] = "prepare_unconfirmed"
        add_event(record, "outcome_unknown", "unusable_response")
        store.update(record)
        error.suggestion = f"Check with 'bmd-run api status {attempt_id}' before anything else."
        raise
    _observe(record, projection)
    store.update(record)
    return {"attempt": projection, "local": _local(record)}


# Compute refuses these before any submission work for the request (authentication, scope,
# binding and admission checks), so the attempt is exactly as it was.
_REFUSED_BEFORE_SUBMISSION = frozenset({
    "unauthenticated", "insufficient_scope", "attempt_forbidden", "loopback_required", "query_not_allowed",
    "invalid_json", "unsupported_media_type", "request_too_large", "invalid_request", "invalid_attempt_id",
    "plan_digest_mismatch", "plan_changed", "attempt_request_mismatch", "resource_limit_exceeded",
    "api_not_configured", "execution_not_configured",
})


def submit(api: ApiTransport, store: AttemptStore, attempt_id: str) -> dict:
    """Submit a recorded attempt: the persisted request, with only ``submit`` set to true."""

    record = _load(store, attempt_id)
    _check_origin(record, api)
    previous_state = record["local_state"]
    previously_requested = record["submit_requested"]
    record["submit_requested"] = True
    record["local_state"] = "submit_unconfirmed" if previous_state not in ("submitted", "submission_uncertain") else previous_state
    add_event(record, "submit_sent")
    store.update(record)  # the intent to submit is durable before the request leaves
    try:
        response = api.call(ApiEndpoint.PUT_ATTEMPT, attempt_id=attempt_id, body=attempt_body(record, submit=True))
    except RequestOutcomeUnknown as unknown:
        add_event(record, "outcome_unknown", "timeout" if unknown.timed_out else "connection_lost")
        store.update(record)
        raise SubmissionUncertain(
            "The submit request got no complete response; the job may or may not have been submitted.",
            suggestion=(
                f"Do not create a new attempt. Check with 'bmd-run api status {attempt_id}', or repeat "
                f"'bmd-run api submit {attempt_id}' (BMD Compute never submits one attempt twice)."
            ),
            details={"attempt_id": attempt_id},
        ) from None
    except UnexpectedResponse as error:
        # The request was sent and an answer arrived, but it is not one the client can read
        # (for example an HTML error page, malformed JSON or a redirect): the job may exist.
        add_event(record, "outcome_unknown", "unusable_response")
        store.update(record)  # stays submit_unconfirmed
        raise SubmissionUncertain(
            "BMD Compute's answer to the submit request could not be read; the job may or may not have been submitted.",
            suggestion=(
                f"Do not create a new attempt. Check with 'bmd-run api status {attempt_id}', or repeat "
                f"'bmd-run api submit {attempt_id}' (BMD Compute never submits one attempt twice)."
            ),
            http_status=error.http_status,
            details={"attempt_id": attempt_id},
        ) from None
    except (ServiceUnavailable, NetworkTimeout) as error:
        record["local_state"] = previous_state
        record["submit_requested"] = previously_requested
        add_event(record, "not_sent")
        store.update(record)
        error.details["attempt_id"] = attempt_id
        error.suggestion = f"Nothing was sent. Retry with: bmd-run api submit {attempt_id}"
        raise
    if response.status != 200:
        error = classify_error(response, scope="submit", attempt_id=attempt_id,
                               expected_plan_digest=record["expected_plan_digest"])
        code = getattr(error, "compute_code", None)
        if code == "attempt_failed" or code is None:
            # A generic Compute failure, or an answer the client cannot read, to a
            # submit request: the job may or may not exist. Report it as uncertain.
            error = SubmissionUncertain(
                "BMD Compute did not report the outcome of the submit request; the job may or may not have been submitted.",
                suggestion=(
                    f"Do not create a new attempt. Check with 'bmd-run api status {attempt_id}', or repeat "
                    f"'bmd-run api submit {attempt_id}' (BMD Compute never submits one attempt twice)."
                ),
                http_status=response.status, compute_code=code, details={"attempt_id": attempt_id},
            )
        if code == "submission_uncertain":
            record["local_state"] = "submission_uncertain"
            attempt = error.details.get("attempt") if isinstance(error, ApiError) else None
            if attempt:
                _observe(record, attempt)
        elif code is not None and code not in ("submission_outcome_unconfirmed", "remote_state_unconfirmed", "attempt_failed"):
            # Refused, or did not start: this request submitted nothing. (An unrecognised
            # answer, code None, leaves the state as submit_unconfirmed.)
            record["local_state"] = previous_state
            if code in _REFUSED_BEFORE_SUBMISSION:
                record["submit_requested"] = previously_requested
        add_event(record, "compute_error", code or error.kind)
        store.update(record)
        if isinstance(error, ApiError):
            error.details["attempt_id"] = attempt_id
        raise error
    try:
        projection = project_attempt(response.document, attempt_id=attempt_id,
                                     expected_plan_digest=record["expected_plan_digest"])
    except UnexpectedResponse as error:
        add_event(record, "outcome_unknown", "unusable_response")
        store.update(record)  # stays submit_unconfirmed: the outcome is not known
        error.suggestion = f"Do not create a new attempt. Check with 'bmd-run api status {attempt_id}'."
        raise
    _observe(record, projection)
    store.update(record)
    return {"attempt": projection, "local": _local(record)}


def status(api: ApiTransport, store: AttemptStore, attempt_id: str) -> dict:
    """``GET /api/v1/attempts/{id}``; updates the local record when one exists."""

    canonical = canonical_attempt_id(attempt_id)
    if canonical is None:
        raise UsageError("The attempt ID must be a canonical lowercase UUID.")
    record = store.load(canonical)
    if record is not None:
        _check_origin(record, api)
    try:
        response = api.call(ApiEndpoint.GET_ATTEMPT, attempt_id=canonical)
    except RequestOutcomeUnknown as unknown:
        raise _lost(unknown, "The status request got no complete response.", "Reading status has no side effects; retry.") from None
    expected = record["expected_plan_digest"] if record is not None else None
    projection = project_attempt(_expect_200(response, scope="read", attempt_id=canonical), attempt_id=canonical,
                                 expected_plan_digest=expected)
    if record is not None:
        add_event(record, "status_read")
        _observe(record, projection)
        store.update(record)
    return {"attempt": projection, "local": _local(record) if record is not None else None}


def _load(store: AttemptStore, attempt_id: str) -> dict:
    canonical = canonical_attempt_id(attempt_id)
    if canonical is None:
        raise UsageError("The attempt ID must be a canonical lowercase UUID.")
    record = store.load(canonical)
    if record is None:
        raise UsageError(
            "There is no local record of this attempt; it cannot be prepared or submitted from here.",
            suggestion="Attempts are resumed only from the record written when they were prepared.",
        )
    return record


def _local(record: dict) -> dict:
    return {
        "local_state": record["local_state"],
        "submit_requested": record["submit_requested"],
        "expected_plan_digest": record["expected_plan_digest"],
        "request_sha256": record["request_sha256"],
        "request": request_summary(record["request"]),
        "structure_source": dict(record["structure_source"]),
    }
