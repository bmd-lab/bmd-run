"""The closed machine-API endpoint table (authenticated capability).

This is the complete list of requests ``bmd-run api ...`` can make to BMD
Compute's machine API v1 (bmd-compute ``docs/machine_api.md``). It is separate
from the frozen v1 read/build table in ``endpoints.py``, which is unchanged.

As there, callers name an operation with an opaque :class:`ApiEndpoint`
member that carries no route data. The method, path template and scope live in
a private read-only mapping of immutable tuples, read only by :func:`resolve`.
The attempt path is built here, and only from a validated canonical lowercase
RFC 4122 UUID, so no caller can choose a path. The authenticated transport
re-checks the result against its own literal allowlist.

Deliberately absent: ``GET /api/v1/identity`` (not needed by R1), and every
route Compute does not offer to machine clients (cancellation, results). No
query string is ever sent.

Adding a route is an authority change and must be reviewed as one.
``tests/test_security_boundary.py`` pins this table.
"""

from __future__ import annotations

import re
import uuid
from enum import Enum
from types import MappingProxyType
from typing import NamedTuple, Optional, Tuple

from .errors import UsageError

_SEALED = False


class ApiEndpoint(Enum):
    """Opaque machine-API operation identifiers. Members carry no method or path."""

    PLANS = "plans"
    PUT_ATTEMPT = "put_attempt"
    GET_ATTEMPT = "get_attempt"

    def __setattr__(self, name, value):
        if _SEALED:
            raise AttributeError("ApiEndpoint members are immutable.")
        super().__setattr__(name, value)

    def __delattr__(self, name):
        raise AttributeError("ApiEndpoint members are immutable.")


_SEALED = True

PLANS_PATH = "/api/v1/plans"
ATTEMPTS_PREFIX = "/api/v1/attempts/"


class ApiRoute(NamedTuple):
    method: str
    path_template: str
    takes_attempt_id: bool
    sends_body: bool
    scope: str


_PLANS_ROUTE = ApiRoute("POST", PLANS_PATH, False, True, "plan")
# The scope of a PUT depends on its body: "prepare" for submit=false, "submit" for submit=true.
_PUT_ROUTE = ApiRoute("PUT", ATTEMPTS_PREFIX + "{attempt_id}", True, True, "prepare_or_submit")
_GET_ROUTE = ApiRoute("GET", ATTEMPTS_PREFIX + "{attempt_id}", True, False, "read")

_ROUTES = MappingProxyType({"plans": _PLANS_ROUTE, "put_attempt": _PUT_ROUTE, "get_attempt": _GET_ROUTE})
_MEMBERS = (
    (ApiEndpoint.PLANS, "plans", _PLANS_ROUTE),
    (ApiEndpoint.PUT_ATTEMPT, "put_attempt", _PUT_ROUTE),
    (ApiEndpoint.GET_ATTEMPT, "get_attempt", _GET_ROUTE),
)

_CANONICAL_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def canonical_attempt_id(value) -> Optional[str]:
    """Return ``value`` if it is a canonical lowercase RFC 4122 UUID, else None (Compute's rule)."""

    if not isinstance(value, str) or not _CANONICAL_UUID.match(value):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    if str(parsed) != value or parsed.variant != uuid.RFC_4122:
        return None
    return value


def resolve(endpoint, attempt_id: Optional[str] = None) -> Tuple[ApiRoute, str]:
    """Return ``(route, path)`` for an ``ApiEndpoint`` member, or refuse."""

    for member, key, route in _MEMBERS:
        if endpoint is member:
            if getattr(member, "_value_", None) != key or _ROUTES.get(key) is not route:
                break
            if route.takes_attempt_id:
                canonical = canonical_attempt_id(attempt_id)
                if canonical is None:
                    raise UsageError("The attempt ID must be a canonical lowercase UUID.")
                return route, ATTEMPTS_PREFIX + canonical
            if attempt_id is not None:
                break
            return route, PLANS_PATH
    raise UsageError("Requests may only target members of the closed machine-API endpoint table.")


def describe() -> Tuple[Tuple[str, str, str, str], ...]:
    """Read-only description: ``(operation, method, path template, scope)``."""

    return tuple((key, route.method, route.path_template, route.scope) for _, key, route in _MEMBERS)
