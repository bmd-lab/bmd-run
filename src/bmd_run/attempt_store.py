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
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Mapping, Optional

from .api_endpoints import canonical_attempt_id
from .errors import LocalStateError

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
        path = self.path_for(attempt_id)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return None
        except OSError:
            raise LocalStateError("The local attempt record could not be opened (it must be a regular file).") from None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise LocalStateError("The local attempt record must be a private regular file owned by you.")
            if info.st_size > MAX_RECORD_BYTES:
                raise _invalid("size")
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
            raise _invalid("size")
        try:
            record = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise _invalid("json") from None
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
