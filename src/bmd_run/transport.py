"""Minimal HTTP transport restricted to the closed endpoint table.

Design constraints:

* The only way to issue a request is ``Transport.request(Endpoint.X, form)``.
  There is no path, URL or method parameter.
* The base URL must be a bare origin (``http(s)://host[:port]``).
* Redirects are never followed; a 3xx response is an error.
* Environment proxy variables are not consulted (``http.client`` does not read
  them), so requests go only to the configured origin.
* Response bodies are size-limited.
"""

from __future__ import annotations

import http.client
import ssl
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from . import CLIENT_NAME, __version__
from .endpoints import Endpoint, Route, resolve
from .errors import ComputeUnreachable, UnexpectedResponse, UsageError

DEFAULT_TIMEOUT_S = 120.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_FIELD_CHARS = 2 * 1024 * 1024


@dataclass(frozen=True)
class Origin:
    scheme: str
    host: str
    port: Optional[int]

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = f":{self.port}" if self.port is not None else ""
        return f"{self.scheme}://{host}{port}"


@dataclass(frozen=True)
class Response:
    endpoint: Endpoint
    route: Route
    status: int
    content_type: str
    text: str


def parse_origin(base_url: str) -> Origin:
    """Validate that ``base_url`` is a bare http(s) origin and return it."""

    if not isinstance(base_url, str) or not base_url.strip():
        raise UsageError(
            "No BMD Compute URL was given.",
            suggestion="Pass --compute-url http://HOST:PORT or set BMD_COMPUTE_URL.",
        )
    raw = base_url.strip()
    try:
        parts = urllib.parse.urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise UsageError(f"Invalid BMD Compute URL: {raw!r} ({exc}).") from exc
    if parts.scheme not in ("http", "https"):
        raise UsageError(
            f"BMD Compute URL must use http or https, not {parts.scheme or 'nothing'!r}."
        )
    if not parts.hostname:
        raise UsageError(f"BMD Compute URL has no host: {raw!r}.")
    if parts.username is not None or parts.password is not None:
        raise UsageError("BMD Compute URL must not contain credentials.")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise UsageError(
            "BMD Compute URL must be an origin only (scheme://host[:port]), "
            "without a path, query or fragment.",
        )
    return Origin(scheme=parts.scheme, host=parts.hostname, port=port)


ConnectionFactory = Callable[[Origin, float], http.client.HTTPConnection]


def default_connection_factory(origin: Origin, timeout: float) -> http.client.HTTPConnection:
    if origin.scheme == "https":
        return http.client.HTTPSConnection(
            origin.host,
            origin.port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    return http.client.HTTPConnection(origin.host, origin.port, timeout=timeout)


class Transport:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        connection_factory: ConnectionFactory = default_connection_factory,
    ) -> None:
        self.origin = parse_origin(base_url)
        if not (isinstance(timeout, (int, float)) and 0 < timeout <= 3600):
            raise UsageError("Timeout must be a number of seconds between 0 and 3600.")
        self.timeout = float(timeout)
        self._connection_factory = connection_factory

    @property
    def base_url(self) -> str:
        return self.origin.url

    def request(self, endpoint: Endpoint, form: Optional[Mapping[str, str]] = None) -> Response:
        route = resolve(endpoint)
        # Second, independent check against literals compiled into this function.
        if (route.method, route.path) not in (
            ("GET", "/openapi.json"),
            ("POST", "/analyze"),
            ("POST", "/build-workflow"),
        ):
            raise UsageError("Requests may only target members of the closed endpoint table.")
        body = _encode_form(route, form)
        headers = {
            "User-Agent": f"{CLIENT_NAME}/{__version__}",
            "Accept": "application/json" if route.method == "GET" else "text/html",
            "Connection": "close",
        }
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        connection = None
        try:
            connection = self._connection_factory(self.origin, self.timeout)
            connection.request(route.method, route.path, body=body, headers=headers)
            raw = connection.getresponse()
            status = int(raw.status)
            content_type = raw.getheader("Content-Type", "") or ""
            data = raw.read(MAX_RESPONSE_BYTES + 1)
        except UsageError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise ComputeUnreachable(
                f"Could not reach BMD Compute at {self.base_url}: {exc}",
                suggestion="Check the URL and that you are on the TAU VPN or university network.",
            ) from exc
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

        if len(data) > MAX_RESPONSE_BYTES:
            raise UnexpectedResponse(
                f"BMD Compute response to {route.method} {route.path} exceeded "
                f"{MAX_RESPONSE_BYTES} bytes."
            )
        if 300 <= status < 400:
            raise UnexpectedResponse(
                f"BMD Compute answered {route.method} {route.path} with a redirect "
                f"(HTTP {status}); the client never follows redirects.",
                http_status=status,
            )
        return Response(
            endpoint=endpoint,
            route=route,
            status=status,
            content_type=content_type,
            text=data.decode("utf-8", errors="replace"),
        )


def _encode_form(route: Route, form: Optional[Mapping[str, str]]) -> Optional[str]:
    form = dict(form or {})
    if route.method == "GET":
        if form:
            raise UsageError(f"{route.path} takes no form fields.")
        return None
    unknown = sorted(set(form) - route.allowed_fields)
    if unknown:
        raise UsageError(
            f"Form fields not allowed for {route.path}: {', '.join(unknown)}."
        )
    missing = sorted(field for field in route.required_fields if not form.get(field))
    if missing:
        raise UsageError(f"Missing required form fields for {route.path}: {', '.join(missing)}.")
    for name, value in form.items():
        if not isinstance(value, str):
            raise UsageError(f"Form field {name!r} must be text.")
        if len(value) > MAX_FIELD_CHARS:
            raise UsageError(f"Form field {name!r} is too large.")
    return urllib.parse.urlencode(sorted(form.items()))
