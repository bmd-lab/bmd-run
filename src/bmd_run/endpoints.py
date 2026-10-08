"""The closed endpoint table.

This is the complete list of HTTP requests the client can make.

Callers name an operation with an opaque :class:`Endpoint` member. The member
carries no route data. The method, path and form-field rules live in a private,
read-only mapping of immutable tuples, which only :func:`resolve` reads. The
transport then checks the resolved route against its own literal allowlist
before sending anything.

Consequences:

* Setting ``Endpoint.OPENAPI.path``, or any other attribute, on a member raises
  and changes nothing.
* Corrupting a member's internal name or value makes resolution fail closed.
* Route tuples cannot be modified, even with ``object.__setattr__``.

Adding a route is an authority change and must be reviewed as one.
``tests/test_security_boundary.py`` pins this table.

Deliberately absent (frozen BMD Compute v1.0.0 routes this client must never
call): prepare, submit, monitor, resume, and the browser home page.
"""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import FrozenSet, NamedTuple, Tuple

from .errors import UsageError

_SEALED = False


class Endpoint(Enum):
    """Opaque operation identifiers. Members carry no method or path."""

    OPENAPI = "openapi"
    ANALYZE = "analyze"
    BUILD_WORKFLOW = "build_workflow"

    def __setattr__(self, name, value):
        if _SEALED:
            raise AttributeError("Endpoint members are immutable.")
        super().__setattr__(name, value)

    def __delattr__(self, name):
        raise AttributeError("Endpoint members are immutable.")


_SEALED = True


class Route(NamedTuple):
    method: str
    path: str
    allowed_fields: FrozenSet[str]
    required_fields: FrozenSet[str]


_OPENAPI_ROUTE = Route("GET", "/openapi.json", frozenset(), frozenset())
_ANALYZE_ROUTE = Route("POST", "/analyze", frozenset({"structure", "fmt"}), frozenset({"structure", "fmt"}))
_BUILD_ROUTE = Route(
    "POST",
    "/build-workflow",
    frozenset({"structure", "fmt", "workflow", "cpus", "memory_gb", "walltime", "queue"}),
    frozenset({"structure", "fmt", "workflow"}),
)

_ROUTES = MappingProxyType(
    {
        "openapi": _OPENAPI_ROUTE,
        "analyze": _ANALYZE_ROUTE,
        "build_workflow": _BUILD_ROUTE,
    }
)
_MEMBERS = (
    (Endpoint.OPENAPI, "openapi", _OPENAPI_ROUTE),
    (Endpoint.ANALYZE, "analyze", _ANALYZE_ROUTE),
    (Endpoint.BUILD_WORKFLOW, "build_workflow", _BUILD_ROUTE),
)


def resolve(endpoint) -> Route:
    """Return the immutable route for an ``Endpoint`` member, or refuse.

    The member must be one of the three original objects (identity check), and
    its internal value must still be the original key. The route object must be
    the original immutable tuple.
    """

    for member, key, route in _MEMBERS:
        if endpoint is member:
            if getattr(member, "_value_", None) != key or _ROUTES.get(key) is not route:
                break
            return route
    raise UsageError("Requests may only target members of the closed endpoint table.")


def describe() -> Tuple[Tuple[str, str, str], ...]:
    """Read-only description of the table: ``(operation, method, path)``."""

    return tuple((key, route.method, route.path) for _, key, route in _MEMBERS)
