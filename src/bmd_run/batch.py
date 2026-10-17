"""Batch execution (Milestone R3A): ``bmd-run batch MANIFEST`` and ``bmd-run batch status MANIFEST``.

A batch is a thin loop over the R1 operations in ``machine.py``. It adds no request
building (the CLI's single-calculation builder is passed in), no new endpoint, no
retry and no scientific choice. What it adds is a campaign record per cell
(``attempt_store.CampaignStore``) so that every cell is bound to exactly one attempt:

1. the manifest is read and every request is built before anything is sent;
   a cell already recorded must still have the identical request and API origin;
2. a cell with no record gets one, created exclusively *before* its attempt UUID
   is used (that is the only new state; it is local and has no remote effect);
3. a cell's attempt is then advanced with the R1 operations only: ``prepare_new``
   (plan, record the attempt, Prepare) for a cell whose attempt was never recorded,
   ``prepare_resume`` for an attempt not yet prepared, ``submit`` for a prepared one;
   a submitted attempt is left alone and an uncertain submission is never repeated;
4. every remaining cell is started, unless ``max_new`` (optional, for testing) bounds
   the new attempts of one invocation; attempts recorded earlier are always
   continued, so running the batch again resumes it;
5. errors specific to one cell (a rejected request, a definite remote failure, an
   uncertain submission) are recorded on the cell and the batch continues; the
   batch stops at the first campaign-wide error (connectivity, credentials, local
   state, or anything else not specific to one cell).

``batch status`` only reads: it asks BMD Compute for each recorded attempt's state
(``GET``), never prepares or submits, and reports one of ``pending``,
``submitted``, ``completed``, ``failed`` or ``uncertain`` per cell.

Memory figures are the memory *requested* for the job, as resolved by BMD Compute;
they are never measured peak memory, and are reported under names that say so.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import CLIENT_NAME, __version__, machine
from . import api_vocabulary as vocab
from .api_transport import ApiTransport
from .attempt_store import CAMPAIGN_ID, CampaignStore, new_cell, request_sha256, utc_now
from .errors import (
    AttemptConflict,
    AttemptNotFound,
    ClientError,
    InvalidRequest,
    PlanDigestMismatch,
    RemoteOperationFailed,
    SubmissionUncertain,
    UsageError,
)
from .machine_output import REFERENCE_COMMIT, error_dict, render_error

MANIFEST_VERSION = 1
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_NEW = None  # no bound: every remaining cell

BATCH_SCHEMA = "bmd_run.batch_output"
BATCH_SCHEMA_VERSION = 1
CATEGORIES = ("pending", "submitted", "completed", "failed", "uncertain")
ACTIONS = ("submitted", "already_submitted", "uncertain_skipped", "not_started", "failed", "stopped", "not_reached")
MEMORY_NOTE = (
    "requested_memory_gb is the memory requested for the job (resolved by BMD Compute), "
    "not measured peak memory; bmd-run does not measure memory use."
)

_MANIFEST_KEYS = frozenset({"version", "campaign", "calculations"})
_CALCULATION_KEYS = frozenset({
    "cell", "structure", "format", "desired_output", "custom_workflow", "cpus", "memory_gb", "walltime", "queue",
})
# Errors that concern one cell's request or attempt: the batch records them and continues.
# An uncertain submission is one cell's: it is recorded and never retried, and independent cells
# continue. Every other error (connectivity, credentials, local state, ...) stops the batch.
CELL_ERRORS = (InvalidRequest, PlanDigestMismatch, AttemptConflict, RemoteOperationFailed, AttemptNotFound,
               SubmissionUncertain)
# A cell whose last error is one of these will not progress without a change by the user.
_PERMANENT_ERROR_KINDS = ("invalid_request", "plan_digest_mismatch", "attempt_conflict")
_UNCERTAIN_STATES = ("submit_unconfirmed", "submission_uncertain")
_NOT_PREPARED_STATES = ("planned", "prepare_unconfirmed", "registered")


# =================================================================== manifest


@dataclass(frozen=True)
class Calculation:
    cell: str
    request: dict
    source: dict
    request_sha: str


@dataclass(frozen=True)
class Manifest:
    campaign: str
    file_name: str
    sha256: str
    calculations: Tuple[Calculation, ...]


def _manifest_error(message: str) -> UsageError:
    return UsageError(message, suggestion="See 'Batch manifest' in the README for the format.")


def load_manifest(path_text: str, build_request: Callable[[argparse.Namespace], tuple]) -> Manifest:
    """Read and check a manifest and build every request (nothing is sent)."""

    path = Path(path_text)
    if not path.is_file():
        raise UsageError(f"Manifest file not found: {path_text}")
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise _manifest_error("The manifest file is larger than 64 MiB.")
    data = path.read_bytes()
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _manifest_error("The manifest is not valid UTF-8 JSON.") from None
    if not isinstance(document, dict) or set(document) != _MANIFEST_KEYS:
        raise _manifest_error('The manifest must be an object with exactly "version", "campaign" and "calculations".')
    if document["version"] != MANIFEST_VERSION or isinstance(document["version"], bool):
        raise _manifest_error(f'The manifest "version" must be {MANIFEST_VERSION}.')
    campaign = document["campaign"]
    if not isinstance(campaign, str) or not CAMPAIGN_ID.match(campaign):
        raise _manifest_error(
            'The "campaign" identifier must be 1-64 characters: letters, digits, ".", "_" or "-", '
            "starting with a letter or digit."
        )
    entries = document["calculations"]
    if not isinstance(entries, list) or not entries:
        raise _manifest_error('"calculations" must be a non-empty list of calculations.')
    base = path.resolve().parent
    seen: Dict[str, str] = {}
    calculations = []
    for number, entry in enumerate(entries, start=1):
        calculations.append(_calculation(number, entry, base, seen, build_request))
    return Manifest(campaign=campaign, file_name=path.name, sha256=hashlib.sha256(data).hexdigest(),
                    calculations=tuple(calculations))


def _calculation(number: int, entry: Any, base: Path, seen: Dict[str, str],
                 build_request: Callable[[argparse.Namespace], tuple]) -> Calculation:
    where = f"Calculation {number}"
    if not isinstance(entry, dict) or not {"cell", "structure"} <= set(entry) <= _CALCULATION_KEYS:
        raise _manifest_error(
            f"{where} must be an object with \"cell\" and \"structure\", and only these other keys: "
            + ", ".join(sorted(_CALCULATION_KEYS - {"cell", "structure"})) + "."
        )
    cell = entry["cell"]
    if not isinstance(cell, str) or not CAMPAIGN_ID.match(cell):
        raise _manifest_error(f'{where}: "cell" must be 1-64 characters: letters, digits, ".", "_" or "-".')
    where = f"Calculation {number} ({cell})"
    folded = cell.lower()  # cell records are files: identifiers differing only in case would collide
    if folded in seen:
        raise _manifest_error(f'{where}: the cell identifier is already used by "{seen[folded]}".')
    seen[folded] = cell
    if ("desired_output" in entry) == ("custom_workflow" in entry):
        raise _manifest_error(f'{where}: give exactly one of "desired_output" or "custom_workflow".')
    strings = {}
    for key in ("structure", "format", "desired_output", "custom_workflow", "walltime", "queue"):
        if key in entry:
            if not isinstance(entry[key], str) or not entry[key] or len(entry[key]) > 4096:
                raise _manifest_error(f'{where}: "{key}" must be a non-empty string.')
            strings[key] = entry[key]
    for key in ("cpus", "memory_gb"):
        if key in entry:
            value = entry[key]
            if not isinstance(value, int) or isinstance(value, bool) or not 0 < value < 10**6:
                raise _manifest_error(f'{where}: "{key}" must be a positive integer.')
            strings[key] = str(value)
    if "format" in strings and strings["format"] not in vocab.STRUCTURE_FORMATS:
        raise _manifest_error(f'{where}: "format" must be "poscar" or "cif".')

    def resolve(key: str) -> Optional[str]:
        return str(base / strings[key]) if key in strings else None

    arguments = argparse.Namespace(
        structure_file=resolve("structure"), format=strings.get("format"),
        desired_output=strings.get("desired_output"), custom_workflow=resolve("custom_workflow"),
        cpus=strings.get("cpus"), memory_gb=strings.get("memory_gb"),
        walltime=strings.get("walltime"), queue=strings.get("queue"),
    )
    try:
        request, source = build_request(arguments)
    except UsageError as error:
        raise UsageError(f"{where}: {error.message}", suggestion=error.suggestion) from None
    return Calculation(cell=cell, request=request, source=source, request_sha=request_sha256(request))


# ================================================================ projections


def plan_summary(plan: dict) -> dict:
    """What a cell keeps from the plan: the client's projection, reduced to benchmark fields."""

    structure, workflow = plan["structure"], plan["workflow"]
    return {
        "plan_digest": plan["plan_digest"],
        "formula": structure["formula"],
        "reduced_formula": structure["reduced_formula"],
        "natoms": structure["natoms"],
        "space_group_number": structure["space_group_number"],
        "crystal_system": structure["crystal_system"],
        "workflow_mode": plan["request"]["workflow_mode"],
        "desired_output": workflow["desired_output"],
        "stage_count": workflow["stage_count"],
        "stages": [{"stage_type": stage["stage_type"], "theory": stage["theory"],
                    "modifiers": list(stage["modifiers"])} for stage in workflow["stages"]],
        "resources": dict(plan["resources"]),
        # Compute R1: which resources Compute selected automatically and which were given.
        **({"resource_sources": dict(plan["resource_selection"]["sources"])} if plan.get("resource_selection") else {}),
    }


def observation(attempt: dict) -> dict:
    """What a cell keeps from an attempt projection: state, job and scheduler report."""

    scheduler = attempt["scheduler"]
    return {
        "state": attempt["state"],
        "job_id": attempt["submission"]["job_id"],
        "scheduler": None if scheduler is None else {
            key: scheduler[key] for key in ("state", "summary", "elapsed", "exit_code", "started_at", "ended_at", "terminal")
        },
        "observed_at": utc_now(),
    }


def category(record: Optional[dict], attempt: Optional[dict]) -> str:
    """The client's classification of a cell (see the module docstring)."""

    error = record["last_error"] if record is not None else None
    stuck = error is not None and error["kind"] in _PERMANENT_ERROR_KINDS
    if attempt is None:
        return "failed" if stuck else "pending"
    state = attempt["local_state"]
    if state in _UNCERTAIN_STATES:
        return "uncertain"
    if state == "submitted":
        scheduler = (record["observed"] or {}).get("scheduler") if record is not None else None
        if scheduler is not None and scheduler["terminal"]:
            return "completed" if scheduler["summary"] == "SUCCESS" else "failed"
        return "submitted"
    return "failed" if stuck else "pending"


def cell_row(calculation: Calculation, record: Optional[dict], attempt: Optional[dict],
             action: Optional[str]) -> dict:
    plan = record["plan"] if record is not None else None
    observed = record["observed"] if record is not None else None
    attempt_id = record["attempt_id"] if record is not None else None
    kind = category(record, attempt)
    recovery = []
    if attempt_id is not None and kind == "uncertain":
        recovery = [f"bmd-run api status {attempt_id}", f"bmd-run api submit {attempt_id}"]
    resources = plan["resources"] if plan is not None else None
    return {
        "cell": calculation.cell,
        "category": kind,
        "action": action,
        "attempt_id": attempt_id,
        "local_state": attempt["local_state"] if attempt is not None else None,
        "plan_digest": plan["plan_digest"] if plan is not None else None,
        "structure_source": dict(calculation.source),
        "structure": None if plan is None else {
            key: plan[key] for key in ("formula", "reduced_formula", "natoms", "space_group_number", "crystal_system")
        },
        "workflow": None if plan is None else {
            "mode": plan["workflow_mode"], "desired_output": plan["desired_output"],
            "stage_count": plan["stage_count"], "stages": [dict(stage) for stage in plan["stages"]],
        },
        "resources": None if resources is None else {
            "nodes": resources["nodes"],
            "cpus": resources["cpus"],
            "requested_memory_gb": resources["memory_gb"],
            "walltime_limit": resources["walltime"],
            "partition": resources["partition"],
            "account": resources["account"],
            **({"qos": resources["qos"]} if "qos" in resources else {}),
            **({"sources": dict(plan["resource_sources"])} if "resource_sources" in plan else {}),
        },
        "job_id": observed["job_id"] if observed is not None else None,
        "attempt_state": observed["state"] if observed is not None else None,
        "scheduler": dict(observed["scheduler"]) if observed is not None and observed["scheduler"] else None,
        "observed_at": observed["observed_at"] if observed is not None else None,
        "last_error": dict(record["last_error"]) if record is not None and record["last_error"] else None,
        "recovery": recovery,
    }


# ===================================================================== checks


def _existing_cells(store: CampaignStore, manifest: Manifest, api_origin: Optional[str]) -> Dict[str, Optional[dict]]:
    records = {calc.cell: store.load_cell(manifest.campaign, calc.cell) for calc in manifest.calculations}
    changed = [calc.cell for calc in manifest.calculations
               if records[calc.cell] is not None and records[calc.cell]["request_sha256"] != calc.request_sha]
    if changed:
        raise UsageError(
            "The request of already recorded cell(s) " + ", ".join(changed) + " no longer matches the manifest "
            "(structure file, workflow or resources changed). Nothing was sent.",
            suggestion="A recorded cell keeps its original request and attempt. Restore the inputs, or give the "
                       "changed calculation a new cell identifier.",
        )
    if api_origin is not None:
        moved = [cell for cell, record in records.items() if record is not None and record["api_origin"] != api_origin]
        if moved:
            raise UsageError(
                "Cell(s) " + ", ".join(moved) + " were recorded against a different BMD Compute API origin. "
                "Nothing was sent.",
                suggestion="Use the --api-url these cells were created with.",
            )
    return records


def _set_error(store: CampaignStore, record: Optional[dict], stage: str, error: ClientError) -> None:
    if record is None:
        return
    record["last_error"] = {"stage": stage, "kind": error.kind,
                            "compute_code": getattr(error, "compute_code", None), "at": utc_now()}
    store.update_cell(record)


# ======================================================================= run


class _Run:
    def __init__(self, api: ApiTransport, store: CampaignStore, manifest: Manifest, max_new: Optional[int],
                 notify: Callable[[str, Any], None]) -> None:
        self.api, self.store, self.manifest, self.max_new, self.notify = api, store, manifest, max_new, notify
        self.new_attempts = 0

    def limit_reached(self) -> bool:
        return self.max_new is not None and self.new_attempts >= self.max_new

    def advance(self, calc: Calculation, record: Optional[dict]) -> Tuple[str, Optional[dict]]:
        """Move one cell forward with the R1 operations. Returns (action, record)."""

        store, campaign = self.store, self.manifest.campaign
        if record is None:
            if self.limit_reached():
                return "not_started", None
            record = new_cell(campaign=campaign, cell=calc.cell, attempt_id=machine.generate_attempt_id(),
                              api_origin=self.api.base_url, request_sha=calc.request_sha, structure_source=calc.source)
            store.create_cell(record)  # bound to its attempt UUID before the UUID is used anywhere
            self.notify("reserved", (calc.cell, record["attempt_id"]))
        attempt_id = record["attempt_id"]
        attempt = store.load(attempt_id)
        stage = "plan"
        try:
            if attempt is None:
                # Never recorded, so never sent to BMD Compute: start it (with the reserved UUID).
                if self.limit_reached():
                    return "not_started", record
                self.new_attempts += 1
                expected = record["plan"]["plan_digest"] if record["plan"] is not None else None

                def planned(plan: dict) -> None:
                    # Keep the first plan: if BMD Compute now resolves a different one, prepare_new
                    # refuses it (expected_plan_digest) and the cell stays bound to the original.
                    if expected is None:
                        record["plan"] = plan_summary(plan)
                        store.update_cell(record)

                machine.prepare_new(self.api, store, calc.request, calc.source, expected_plan_digest=expected,
                                    new_attempt_id=lambda: attempt_id, on_planned=planned)
            else:
                state = attempt["local_state"]
                if state == "submitted":
                    return "already_submitted", record
                if state in _UNCERTAIN_STATES:
                    return "uncertain_skipped", record  # never resubmitted automatically
                if state in _NOT_PREPARED_STATES:
                    stage = "prepare"
                    machine.prepare_resume(self.api, store, attempt_id)
            stage = "submit"
            result = machine.submit(self.api, store, attempt_id)
        except ClientError as error:
            if stage == "plan" and store.load(attempt_id) is not None:
                stage = "prepare"  # the plan succeeded and the attempt was recorded
            _set_error(store, record, stage, error)
            raise
        record["observed"] = observation(result["attempt"])
        record["last_error"] = None
        store.update_cell(record)
        return "submitted", record


def run_batch(api: ApiTransport, store: CampaignStore, manifest: Manifest, *, max_new: Optional[int] = DEFAULT_MAX_NEW,
              notify: Optional[Callable[[str, Any], None]] = None) -> dict:
    """Advance every cell in manifest order. Returns rows, counts and the error that stopped it (if any)."""

    notify = notify or (lambda event, value: None)
    # One invocation per campaign: the lock is taken before any campaign state is read and
    # held until every cell has been handled (released on any exit, including Ctrl-C).
    with store.campaign_lock(manifest.campaign):
        records = _existing_cells(store, manifest, api.base_url)  # before any remote side effect
        run = _Run(api, store, manifest, max_new, notify)
        rows: List[dict] = []
        stopped: Optional[BaseException] = None
        first_cell_error: Optional[ClientError] = None
        for calc in manifest.calculations:
            record = records[calc.cell]
            if stopped is not None:
                action = "not_reached"
            else:
                try:
                    action, record = run.advance(calc, record)
                except CELL_ERRORS as error:
                    action, first_cell_error = "failed", first_cell_error or error
                except (ClientError, KeyboardInterrupt) as error:
                    action, stopped = "stopped", error
                if record is None:
                    record = store.load_cell(manifest.campaign, calc.cell)
            attempt = store.load(record["attempt_id"]) if record is not None else None
            row = cell_row(calc, record, attempt, action)
            rows.append(row)
            notify("cell", row)
    return {"rows": rows, "new_attempts": run.new_attempts, "stopped": stopped, "cell_error": first_cell_error}


# ==================================================================== status

# Errors after which further status requests would fail the same way.
_STATUS_STOPPERS = ("service_unavailable", "network_timeout", "authentication_failed", "insufficient_scope",
                    "credentials_unavailable", "local_state_error")


def batch_status(api: Optional[ApiTransport], store: CampaignStore, manifest: Manifest) -> dict:
    """Read-only: refresh each recorded attempt from BMD Compute (unless ``api`` is None) and classify it."""

    rows: List[dict] = []
    warnings: List[str] = []
    stopped: Optional[ClientError] = None
    for calc in manifest.calculations:
        record = store.load_cell(manifest.campaign, calc.cell)
        if record is not None and record["request_sha256"] != calc.request_sha:
            warnings.append(f"Cell {calc.cell}: the manifest entry no longer matches the recorded request; "
                            "the recorded attempt is reported.")
        attempt = store.load(record["attempt_id"]) if record is not None else None
        if (api is not None and stopped is None and attempt is not None
                and attempt["local_state"] != "planned"):  # a 'planned' attempt was never sent
            try:
                result = machine.status(api, store, record["attempt_id"])
            except AttemptNotFound:
                warnings.append(f"Cell {calc.cell}: BMD Compute has no attempt {record['attempt_id']} yet.")
            except ClientError as error:
                if error.kind in _STATUS_STOPPERS:
                    stopped = error
                    warnings.append("Status could not be refreshed from BMD Compute from cell "
                                    f"{calc.cell} on; the remaining cells show the last local observation.")
                else:
                    warnings.append(f"Cell {calc.cell}: status not refreshed ({error.kind}).")
            else:
                record["observed"] = observation(result["attempt"])
                store.update_cell(record)
                attempt = store.load(record["attempt_id"])
        rows.append(cell_row(calc, record, attempt, None))
    return {"rows": rows, "warnings": warnings, "stopped": stopped}


# ==================================================================== output


def counts(rows: List[dict]) -> dict:
    return {name: sum(1 for row in rows if row["category"] == name) for name in CATEGORIES}


def envelope(*, command: str, api_url: Optional[str], manifest: Optional[Manifest], options: dict,
             rows: List[dict], new_attempts: Optional[int], error: Optional[ClientError],
             warnings: Optional[List[str]] = None) -> dict:
    return {
        "schema": BATCH_SCHEMA,
        "schema_version": BATCH_SCHEMA_VERSION,
        "client": {"name": CLIENT_NAME, "version": __version__},
        "command": command,
        "ok": error is None,
        "compute_api": {"origin": api_url, "api_version": vocab.API_VERSION, "reference_commit": REFERENCE_COMMIT},
        "campaign": manifest.campaign if manifest is not None else None,
        "manifest": None if manifest is None else {
            "file_name": manifest.file_name, "sha256": manifest.sha256,
            "calculation_count": len(manifest.calculations),
        },
        "options": options,
        "summary": {**counts(rows), "new_attempts": new_attempts},
        "cells": rows,
        "memory_note": MEMORY_NOTE,
        "error": error_dict(error) if error is not None else None,
        "warnings": list(warnings or []),
    }


def to_json(document: dict) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


_ACTION_TEXT = {
    "submitted": "submitted",
    "already_submitted": "already submitted",
    "uncertain_skipped": "submission uncertain; not repeated",
    "not_started": "not started (new-attempt limit reached)",
    "failed": "failed",
    "stopped": "stopped",
    "not_reached": "not reached",
}


def render_cell_progress(row: dict) -> str:
    text = f"  {row['cell']:<24} {_ACTION_TEXT.get(row['action'], row['category'])}"
    if row["attempt_id"]:
        text += f"  attempt {row['attempt_id']}"
    if row["job_id"]:
        text += f"  SLURM job {row['job_id']}"
    if row["last_error"] and row["action"] in ("failed", "stopped"):
        error = row["last_error"]
        text += f"  ({error['stage']}: {error['compute_code'] or error['kind']})"
    for command in row["recovery"]:
        text += f"\n      {command}"
    return text


def render_run_header(manifest: Manifest, max_new: Optional[int]) -> str:
    count = len(manifest.calculations)
    text = f"BMD Run batch: campaign {manifest.campaign}, {count} calculation{'s' if count != 1 else ''}"
    if max_new is not None:
        text += f", at most {max_new} new attempt{'s' if max_new != 1 else ''} this run"
    return text


def render_summary(rows: List[dict], new_attempts: Optional[int]) -> str:
    total = counts(rows)
    text = "Cells: " + ", ".join(f"{total[name]} {name}" for name in CATEGORIES)
    if new_attempts is not None:
        text += f"; {new_attempts} new attempt{'s' if new_attempts != 1 else ''} started"
    return text


def render_run_footer(manifest_path: str, stopped: Optional[BaseException]) -> str:
    lines = [""]
    if isinstance(stopped, ClientError):
        lines += [render_error(stopped), "  The batch stopped here; recorded attempts are kept and reused."]
    elif stopped is not None:
        lines.append("Interrupted. Recorded attempts are kept and reused; check them before anything else.")
    lines += [f"Continue:  bmd-run batch {manifest_path}", f"Status:    bmd-run batch status {manifest_path}"]
    return "\n".join(lines)


def _cell_text(value: Any) -> str:
    return "-" if value is None else str(value)


def render_status(manifest: Manifest, rows: List[dict], refreshed: bool, warnings: List[str]) -> str:
    source = "refreshed from BMD Compute" if refreshed else "local records only"
    lines = [f"BMD Run batch status: campaign {manifest.campaign} ({source})", ""]
    header = ("CELL", "STATUS", "SLURM JOB", "SCHEDULER", "ELAPSED", "CPUS", "REQ MEM GB", "WALLTIME", "ATTEMPT")
    table = [header]
    for row in rows:
        scheduler, resources = row["scheduler"] or {}, row["resources"] or {}
        table.append((
            row["cell"], row["category"], _cell_text(row["job_id"]), _cell_text(scheduler.get("state")),
            _cell_text(scheduler.get("elapsed")), _cell_text(resources.get("cpus")),
            _cell_text(resources.get("requested_memory_gb")), _cell_text(resources.get("walltime_limit")),
            _cell_text(row["attempt_id"]),
        ))
    widths = [max(len(line[i]) for line in table) for i in range(len(header))]
    lines += ["  ".join(value.ljust(widths[i]) for i, value in enumerate(line)).rstrip() for line in table]
    for row in rows:
        for command in row["recovery"]:
            lines.append(f"  {row['cell']}: {command}")
    lines += ["", render_summary(rows, None),
              "REQ MEM GB is the memory requested for the job, not measured peak memory."]
    lines += [f"warning: {text}" for text in warnings]
    return "\n".join(lines)
