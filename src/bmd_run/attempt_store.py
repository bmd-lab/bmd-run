"""Protected local store of machine-API attempts (the only module that writes files).

Each attempt is one JSON file, ``<state dir>/attempts/<attempt_id>.json``.
The state directory is ``--state-dir``, else ``BMD_RUN_STATE_DIR``, else
``$XDG_STATE_HOME/bmd-run``, else ``~/.local/state/bmd-run``.

Safety rules:

* the state directory and ``attempts/`` are created with mode 0700 and must be
  real directories (not links) owned by the current user with no group or other
  permission bits, otherwise nothing is read or written;
* a new record is created exclusively (``O_CREAT | O_EXCL``, mode 0600), so an
  attempt ID is never silently reused; updates are written to a new temporary
  file in the same directory and atomically renamed over the record;
* records hold only what is needed to resume: the attempt ID, the Compute API
  origin, the exact scientific request (or nothing), its SHA-256, the expected
  plan digest and the locally observed state. They never hold credentials,
  SSH settings, remote paths or submission identity tokens (the closed record
  schema in :func:`validate_record` refuses any other field);
* a record whose request no longer matches its recorded SHA-256 is refused.
  This detects accidental corruption and inconsistent (partial) edits. It is a
  consistency check, not tamper protection: someone who controls the account
  can edit a request and recompute its SHA-256, and such a record passes.
  Deliberate modification by the record's owner is outside the threat model;
  once Compute has registered an attempt UUID, Compute's own immutable binding
  of that UUID to its request is the authority and refuses a changed request.

Batch campaigns (``bmd-run batch``) keep one small record per cell,
``<state dir>/campaigns/<campaign>/<cell>.json``, under the same rules. A cell
record is created exclusively *before* its attempt UUID is used anywhere, so a
cell is bound to exactly one attempt: a second run (or a concurrent one) finds
the record and reuses that attempt instead of creating another. Cell records
hold identifiers, the request's SHA-256 (the request itself is in the attempt
record), the client's projection of the plan (structure summary, resolved
workflow, requested resources) and the last observed attempt and scheduler
state; nothing else (see :func:`validate_cell`).

Only one ``bmd-run batch`` may advance a campaign at a time. It holds an exclusive
``flock`` on ``<state dir>/campaigns/<campaign>.lock`` for the whole run
(:meth:`CampaignStore.campaign_lock`). The lock belongs to the campaign's state, not
to a manifest path, and the operating system releases it when the process exits,
however it exits. The lock file itself is never removed; it holds no data.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional

from .api_endpoints import canonical_attempt_id
from .errors import LocalStateError

try:  # POSIX advisory locks; the attempt store refuses to run on other systems (see ensure)
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

STATE_DIR_ENV = "BMD_RUN_STATE_DIR"
RECORD_SCHEMA = "bmd_run.attempt_record"
RECORD_SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 200

LOCAL_STATES = (
    "planned",              # record persisted; the prepare request has not been answered yet
    "prepare_unconfirmed",  # the prepare request may have reached Compute; outcome unknown
    "registered",           # Compute states, as last observed
    "prepared",
    "submit_unconfirmed",   # a submit request was sent; outcome not yet observed
    "submitted",
    "submission_uncertain",
)
EVENTS = (
    "created",
    "prepare_sent",
    "submit_sent",
    "status_read",
    "observed",
    "not_sent",
    "outcome_unknown",
    "compute_error",
)

_RECORD_KEYS = frozenset({
    "schema", "schema_version", "attempt_id", "api_origin", "created_at", "updated_at",
    "request", "request_sha256", "structure_source", "expected_plan_digest",
    "local_state", "submit_requested", "last_observed", "events",
})
_REQUEST_KEYS = frozenset({"structure", "workflow", "resources"})
_SOURCE_KEYS = frozenset({"file_name", "sha256", "format"})
_OBSERVED_KEYS = frozenset({"state", "job_id", "scheduler_summary", "observed_at"})
_EVENT_KEYS = frozenset({"at", "event", "code"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PLAN_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_ORIGIN = re.compile(r"^https?://[A-Za-z0-9.:\[\]-]{1,255}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_JOB_ID = re.compile(r"^[0-9]{1,20}(?:_[0-9]{1,10})?$")
_FILE_NAME = re.compile(r"^[^/\\\x00-\x1f]{1,255}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_sha256(request: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest()


def default_state_dir(environ: Mapping[str, str]) -> Path:
    explicit = environ.get(STATE_DIR_ENV)
    if explicit:
        return Path(explicit).expanduser()
    xdg = environ.get("XDG_STATE_HOME")
    if xdg and Path(xdg).is_absolute():
        return Path(xdg) / "bmd-run"
    return Path.home() / ".local" / "state" / "bmd-run"


def new_record(*, attempt_id: str, api_origin: str, request: dict, structure_source: dict,
               expected_plan_digest: str) -> dict:
    now = utc_now()
    record = {
        "schema": RECORD_SCHEMA,
        "schema_version": RECORD_SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "api_origin": api_origin,
        "created_at": now,
        "updated_at": now,
        "request": request,
        "request_sha256": request_sha256(request),
        "structure_source": structure_source,
        "expected_plan_digest": expected_plan_digest,
        "local_state": "planned",
        "submit_requested": False,
        "last_observed": None,
        "events": [{"at": now, "event": "created", "code": None}],
    }
    validate_record(record)
    return record


def add_event(record: dict, event: str, code: Optional[str] = None) -> None:
    if event not in EVENTS:
        raise LocalStateError("Internal error: unknown attempt event.")
    record["events"] = (record["events"] + [{"at": utc_now(), "event": event, "code": code}])[-MAX_EVENTS:]
    record["updated_at"] = utc_now()


def _invalid(detail: str) -> LocalStateError:
    return LocalStateError(
        f"The local attempt record is damaged or inconsistent ({detail}); it was not used.",
        suggestion="Do not edit attempt records by hand. Check the attempt with 'bmd-run api status'.",
    )


def validate_record(record: Any) -> None:
    """Closed-schema check of a record. Raises :class:`LocalStateError`."""

    if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
        raise _invalid("fields")
    if record["schema"] != RECORD_SCHEMA or record["schema_version"] != RECORD_SCHEMA_VERSION:
        raise _invalid("schema")
    if canonical_attempt_id(record["attempt_id"]) is None:
        raise _invalid("attempt_id")
    if not isinstance(record["api_origin"], str) or not _ORIGIN.match(record["api_origin"]):
        raise _invalid("api_origin")
    for key in ("created_at", "updated_at"):
        if not isinstance(record[key], str) or not _UTC.match(record[key]):
            raise _invalid(key)
    request = record["request"]
    if not isinstance(request, dict) or not set(request) <= _REQUEST_KEYS or not {"structure", "workflow"} <= set(request):
        raise _invalid("request")
    if record["request_sha256"] != request_sha256(request):
        raise _invalid("request_sha256")
    source = record["structure_source"]
    if (
        not isinstance(source, dict) or set(source) != _SOURCE_KEYS
        or not isinstance(source["file_name"], str) or not _FILE_NAME.match(source["file_name"])
        or not isinstance(source["sha256"], str) or not _SHA256.match(source["sha256"])
        or source["format"] not in ("poscar", "cif")
    ):
        raise _invalid("structure_source")
    if not isinstance(record["expected_plan_digest"], str) or not _PLAN_DIGEST.match(record["expected_plan_digest"]):
        raise _invalid("expected_plan_digest")
    if record["local_state"] not in LOCAL_STATES:
        raise _invalid("local_state")
    if not isinstance(record["submit_requested"], bool):
        raise _invalid("submit_requested")
    observed = record["last_observed"]
    if observed is not None:
        if not isinstance(observed, dict) or set(observed) != _OBSERVED_KEYS:
            raise _invalid("last_observed")
        if observed["job_id"] is not None and not (isinstance(observed["job_id"], str) and _JOB_ID.match(observed["job_id"])):
            raise _invalid("last_observed.job_id")
        for key in ("state", "scheduler_summary"):
            if observed[key] is not None and not (isinstance(observed[key], str) and _CODE.match(observed[key].lower())):
                raise _invalid(f"last_observed.{key}")
        if not isinstance(observed["observed_at"], str) or not _UTC.match(observed["observed_at"]):
            raise _invalid("last_observed.observed_at")
    events = record["events"]
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise _invalid("events")
    for event in events:
        if (
            not isinstance(event, dict) or set(event) != _EVENT_KEYS or event["event"] not in EVENTS
            or not isinstance(event["at"], str) or not _UTC.match(event["at"])
            or (event["code"] is not None and not (isinstance(event["code"], str) and _CODE.match(event["code"])))
        ):
            raise _invalid("events")


class AttemptStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.attempts = self.root / "attempts"

    # --- directory safety -------------------------------------------------------

    def _check_directory(self, path: Path) -> None:
        try:
            info = os.lstat(path)
        except OSError:
            raise LocalStateError("The attempt state directory could not be inspected.") from None
        if not stat.S_ISDIR(info.st_mode):
            raise LocalStateError(
                f"The attempt state location {path} is not a directory (links are not followed)."
            )
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise LocalStateError(
                f"The attempt state directory {path} must be owned by you and not accessible to others.",
                suggestion=f"Run: chmod 700 {path}",
            )

    def _ensure_dir(self, path: Path) -> None:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        except OSError:
            raise LocalStateError(f"The attempt state directory {path} could not be created.") from None
        self._check_directory(path)

    def ensure(self) -> None:
        if os.name != "posix":
            raise LocalStateError(
                "Machine-API attempts can only be recorded on POSIX systems, where the "
                "state directory's permissions can be checked."
            )
        if not self.root.parent.is_dir():
            raise LocalStateError(f"The parent of the attempt state directory {self.root} does not exist.")
        self._ensure_dir(self.root)
        self._ensure_dir(self.attempts)

    def path_for(self, attempt_id: str) -> Path:
        canonical = canonical_attempt_id(attempt_id)
        if canonical is None:
            raise LocalStateError("Invalid attempt ID.")
        return self.attempts / f"{canonical}.json"

    # --- reading ----------------------------------------------------------------

    def exists(self, attempt_id: str) -> bool:
        self.ensure()
        try:
            os.lstat(self.path_for(attempt_id))
        except FileNotFoundError:
            return False
        return True

    def load(self, attempt_id: str) -> Optional[dict]:
        self.ensure()
        record = _read_private_json(self.path_for(attempt_id), "attempt", _invalid)
        if record is None:
            return None
        validate_record(record)
        if record["attempt_id"] != attempt_id:
            raise _invalid("attempt_id")
        return record

    # --- writing ----------------------------------------------------------------

    def create(self, record: dict) -> Path:
        """Persist a new record exclusively; refuses if the attempt ID is already recorded."""

        validate_record(record)
        self.ensure()
        path = self.path_for(record["attempt_id"])
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            raise LocalStateError("A local record for this attempt ID already exists; it was not replaced.") from None
        except OSError:
            raise LocalStateError("The local attempt record could not be created.") from None
        self._write_all(descriptor, record)
        return path

    def update(self, record: dict) -> None:
        """Atomically replace an existing record."""

        validate_record(record)
        self.ensure()
        path = self.path_for(record["attempt_id"])
        temporary = self.attempts / f".{record['attempt_id']}.{os.getpid()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(temporary, flags, 0o600)
        except OSError:
            raise LocalStateError("The local attempt record could not be updated.") from None
        self._write_all(descriptor, record)
        try:
            os.replace(temporary, path)
        except OSError:
            raise LocalStateError("The local attempt record could not be updated.") from None

    @staticmethod
    def _write_all(descriptor: int, record: dict) -> None:
        data = (json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        except OSError:
            raise LocalStateError("The local attempt record could not be written.") from None
        finally:
            os.close(descriptor)


def _read_private_json(path: Path, what: str, invalid: Callable[[str], LocalStateError]) -> Optional[Any]:
    """Read a private regular JSON file owned by the user; None if it does not exist."""

    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise LocalStateError(f"The local {what} record could not be opened (it must be a regular file).") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise LocalStateError(f"The local {what} record must be a private regular file owned by you.")
        if info.st_size > MAX_RECORD_BYTES:
            raise invalid("size")
        chunks: List[bytes] = []
        remaining = MAX_RECORD_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    if len(data) > MAX_RECORD_BYTES:
        raise invalid("size")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise invalid("json") from None


# ===================================================================== campaigns

CELL_SCHEMA = "bmd_run.campaign_cell"
CELL_SCHEMA_VERSION = 1
# Campaign and cell identifiers: also Compute's label format, minus ':' so they are safe file names.
CAMPAIGN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ERROR_STAGES = ("plan", "prepare", "submit")

_CELL_KEYS = frozenset({
    "schema", "schema_version", "campaign", "cell", "attempt_id", "api_origin", "created_at", "updated_at",
    "request_sha256", "structure_source", "plan", "observed", "last_error",
})
_PLAN_KEYS = frozenset({
    "plan_digest", "formula", "reduced_formula", "natoms", "space_group_number", "crystal_system",
    "workflow_mode", "desired_output", "stage_count", "stages", "resources",
})
_STAGE_KEYS = frozenset({"stage_type", "theory", "modifiers"})
_RESOURCE_KEYS = frozenset({"nodes", "cpus", "memory_gb", "walltime", "partition", "account"})
_CELL_OBSERVED_KEYS = frozenset({"state", "job_id", "scheduler", "observed_at"})
_SCHEDULER_KEYS = frozenset({"state", "summary", "elapsed", "exit_code", "started_at", "ended_at", "terminal"})
_ERROR_KEYS = frozenset({"stage", "kind", "compute_code", "at"})
_FORMULA = re.compile(r"^[A-Z][a-z]?[0-9.e+-]*(?: ?[A-Z][a-z]?[0-9.e+-]*){0,119}$")
_WORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_WALLTIME = re.compile(r"^[0-9]{1,3}:[0-5][0-9]:[0-5][0-9]$")
_ELAPSED = re.compile(r"^(?:[0-9]{1,4}-)?[0-9]{2}:[0-5][0-9]:[0-5][0-9]$")
_EXIT = re.compile(r"^[0-9]{1,3}:[0-9]{1,3}$")
_LOCAL_TIME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}$")


def _cell_invalid(detail: str) -> LocalStateError:
    return LocalStateError(
        f"The local campaign cell record is damaged or inconsistent ({detail}); it was not used.",
        suggestion="Do not edit campaign records by hand.",
    )


def new_cell(*, campaign: str, cell: str, attempt_id: str, api_origin: str, request_sha: str,
             structure_source: dict) -> dict:
    now = utc_now()
    record = {
        "schema": CELL_SCHEMA,
        "schema_version": CELL_SCHEMA_VERSION,
        "campaign": campaign,
        "cell": cell,
        "attempt_id": attempt_id,
        "api_origin": api_origin,
        "created_at": now,
        "updated_at": now,
        "request_sha256": request_sha,
        "structure_source": dict(structure_source),
        "plan": None,
        "observed": None,
        "last_error": None,
    }
    validate_cell(record)
    return record


def _string(value, pattern, nullable=False) -> bool:
    if value is None:
        return nullable
    return isinstance(value, str) and bool(pattern.match(value))


def _integer(value, low, high, nullable=False) -> bool:
    if value is None:
        return nullable
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _closed(value, keys) -> bool:
    return isinstance(value, dict) and set(value) == keys


def _valid_plan(plan) -> bool:
    if plan is None:
        return True
    if not _closed(plan, _PLAN_KEYS) or not _closed(plan["resources"], _RESOURCE_KEYS):
        return False
    resources, stages = plan["resources"], plan["stages"]
    return (
        _string(plan["plan_digest"], re.compile(r"^sha256:[0-9a-f]{64}$"))
        and _string(plan["formula"], _FORMULA) and _string(plan["reduced_formula"], _FORMULA)
        and _integer(plan["natoms"], 1, 10**6)
        and _integer(plan["space_group_number"], 1, 230, nullable=True)
        and _string(plan["crystal_system"], _WORD, nullable=True)
        and plan["workflow_mode"] in ("desired_output", "custom")
        and _string(plan["desired_output"], _WORD, nullable=True)
        and _integer(plan["stage_count"], 1, 64)
        and isinstance(stages, list) and len(stages) == plan["stage_count"]
        and all(
            _closed(stage, _STAGE_KEYS)
            and _string(stage["stage_type"], _WORD, nullable=True) and _string(stage["theory"], _WORD, nullable=True)
            and isinstance(stage["modifiers"], list) and len(stage["modifiers"]) <= 32
            and all(_string(item, _WORD) for item in stage["modifiers"])
            for stage in stages
        )
        and _integer(resources["nodes"], 1, 10**4) and _integer(resources["cpus"], 1, 10**6)
        and _integer(resources["memory_gb"], 1, 10**6) and _string(resources["walltime"], _WALLTIME)
        and _string(resources["partition"], _WORD, nullable=True)
        and _string(resources["account"], _WORD, nullable=True)
    )


def _valid_observed(observed) -> bool:
    if observed is None:
        return True
    if not _closed(observed, _CELL_OBSERVED_KEYS):
        return False
    scheduler = observed["scheduler"]
    return (
        _string(observed["state"], _CODE)
        and _string(observed["job_id"], _JOB_ID, nullable=True)
        and _string(observed["observed_at"], _UTC)
        and (scheduler is None or (
            _closed(scheduler, _SCHEDULER_KEYS)
            and _string(scheduler["state"], _WORD) and _string(scheduler["summary"], _WORD)
            and _string(scheduler["elapsed"], _ELAPSED, nullable=True)
            and _string(scheduler["exit_code"], _EXIT, nullable=True)
            and _string(scheduler["started_at"], _LOCAL_TIME, nullable=True)
            and _string(scheduler["ended_at"], _LOCAL_TIME, nullable=True)
            and isinstance(scheduler["terminal"], bool)
        ))
    )


def validate_cell(record: Any) -> None:
    """Closed-schema check of a campaign cell record. Raises :class:`LocalStateError`."""

    if not _closed(record, _CELL_KEYS):
        raise _cell_invalid("fields")
    if record["schema"] != CELL_SCHEMA or record["schema_version"] != CELL_SCHEMA_VERSION:
        raise _cell_invalid("schema")
    if not _string(record["campaign"], CAMPAIGN_ID) or not _string(record["cell"], CAMPAIGN_ID):
        raise _cell_invalid("identifiers")
    if canonical_attempt_id(record["attempt_id"]) != record["attempt_id"]:
        raise _cell_invalid("attempt_id")
    if not _string(record["api_origin"], _ORIGIN):
        raise _cell_invalid("api_origin")
    if not _string(record["created_at"], _UTC) or not _string(record["updated_at"], _UTC):
        raise _cell_invalid("timestamps")
    if not _string(record["request_sha256"], _SHA256):
        raise _cell_invalid("request_sha256")
    source = record["structure_source"]
    if (
        not _closed(source, _SOURCE_KEYS) or not _string(source["file_name"], _FILE_NAME)
        or not _string(source["sha256"], _SHA256) or source["format"] not in ("poscar", "cif")
    ):
        raise _cell_invalid("structure_source")
    if not _valid_plan(record["plan"]):
        raise _cell_invalid("plan")
    if not _valid_observed(record["observed"]):
        raise _cell_invalid("observed")
    error = record["last_error"]
    if error is not None and not (
        _closed(error, _ERROR_KEYS) and error["stage"] in ERROR_STAGES and _string(error["kind"], _CODE)
        and _string(error["compute_code"], _CODE, nullable=True) and _string(error["at"], _UTC)
    ):
        raise _cell_invalid("last_error")


class CampaignStore(AttemptStore):
    """Cell records of batch campaigns, beside the attempt records and under the same rules."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.campaigns = self.root / "campaigns"

    def _campaign_dir(self, campaign: str) -> Path:
        if not _string(campaign, CAMPAIGN_ID):
            raise LocalStateError("Invalid campaign identifier.")
        return self.campaigns / campaign

    def cell_path(self, campaign: str, cell: str) -> Path:
        if not _string(cell, CAMPAIGN_ID):
            raise LocalStateError("Invalid cell identifier.")
        return self._campaign_dir(campaign) / f"{cell}.json"

    def campaign_lock(self, campaign: str) -> "CampaignLock":
        """Exclusive, non-blocking lock on one campaign's state (a context manager)."""

        return CampaignLock(self, campaign)

    def ensure_campaign(self, campaign: str) -> None:
        self.ensure()
        self._ensure_dir(self.campaigns)
        self._ensure_dir(self._campaign_dir(campaign))

    def load_cell(self, campaign: str, cell: str) -> Optional[dict]:
        """The cell's record, or None. Reading never creates the campaign directory."""

        directory = self._campaign_dir(campaign)
        for path in (self.root, self.campaigns, directory):
            try:
                os.lstat(path)
            except FileNotFoundError:
                return None
            except OSError:
                raise LocalStateError("The campaign state directory could not be inspected.") from None
            self._check_directory(path)
        record = _read_private_json(self.cell_path(campaign, cell), "campaign cell", _cell_invalid)
        if record is None:
            return None
        validate_cell(record)
        if record["campaign"] != campaign or record["cell"] != cell:
            raise _cell_invalid("identifiers")
        return record

    def create_cell(self, record: dict) -> None:
        """Persist a new cell record exclusively; refuses if the cell already has one."""

        validate_cell(record)
        self.ensure_campaign(record["campaign"])
        path = self.cell_path(record["campaign"], record["cell"])
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            raise LocalStateError(
                f"Cell {record['cell']} of campaign {record['campaign']} was recorded by another run while "
                "this one was starting; nothing was sent for it.",
                suggestion="Run only one 'bmd-run batch' per campaign at a time, then run it again.",
            ) from None
        except OSError:
            raise LocalStateError("The campaign cell record could not be created.") from None
        self._write_all(descriptor, record)

    def update_cell(self, record: dict) -> None:
        """Atomically replace an existing cell record."""

        record["updated_at"] = utc_now()
        validate_cell(record)
        self.ensure_campaign(record["campaign"])
        path = self.cell_path(record["campaign"], record["cell"])
        temporary = path.parent / f".{record['cell']}.{os.getpid()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(temporary, flags, 0o600)
        except OSError:
            raise LocalStateError("The campaign cell record could not be updated.") from None
        self._write_all(descriptor, record)
        try:
            os.replace(temporary, path)
        except OSError:
            raise LocalStateError("The campaign cell record could not be updated.") from None


class CampaignLock:
    """``with store.campaign_lock(campaign):`` holds an exclusive ``flock`` on the campaign.

    Acquiring never waits: if another process (or another open of the lock in this
    process) holds it, :class:`LocalStateError` is raised and nothing else happens.
    Closing the descriptor, on leaving the block or when the process ends for any
    reason, releases it.
    """

    def __init__(self, store: CampaignStore, campaign: str) -> None:
        self.store = store
        self.campaign = campaign
        self.descriptor: Optional[int] = None

    def __enter__(self) -> "CampaignLock":
        store = self.store
        store._campaign_dir(self.campaign)  # validates the identifier
        if fcntl is None:  # pragma: no cover
            raise LocalStateError("Campaign locking needs a POSIX system.")
        store.ensure()
        store._ensure_dir(store.campaigns)
        path = store.campaigns / f"{self.campaign}.lock"
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError:
            raise LocalStateError("The campaign lock file could not be opened (it must be a regular file).") from None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise LocalStateError("The campaign lock file must be a private regular file owned by you.")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LocalStateError(
                    f"Campaign {self.campaign} is being advanced by another 'bmd-run batch' right now; "
                    "nothing was sent or recorded by this one.",
                    suggestion="Wait for that run to finish ('bmd-run batch status MANIFEST' works meanwhile), "
                               "then run the batch again.",
                ) from None
            except OSError:
                raise LocalStateError("The campaign lock could not be taken.") from None
        except BaseException:
            os.close(descriptor)
            raise
        self.descriptor = descriptor
        return self

    def __exit__(self, *exc) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)  # releases the flock
            self.descriptor = None
