"""Authenticated JSON transport for the closed machine-API table.

Design constraints:

* The only way to issue a request is ``ApiTransport.call(ApiEndpoint.X, ...)``.
  There is no URL, path or method parameter; attempt paths are built by
  ``api_endpoints.resolve`` from a validated UUID, then re-checked here against
  literal method/path shapes.
* The bearer token goes only into the ``Authorization`` header. No query string
  is ever sent, redirects are never followed, environment proxy variables are
  not consulted (``http.client`` ignores them) and responses are size-limited.
* Credential-bearing requests are only sent over a reviewed transport:

  - ``http://127.0.0.1:PORT`` or ``http://[::1]:PORT``: the loopback end of the
    user's existing SSH tunnel to the Compute host (default
    ``http://127.0.0.1:18000``). Host names such as ``localhost`` are refused,
    because they can resolve elsewhere;
  - ``https://...`` with full certificate and host-name verification, only when
    ``BMD_RUN_API_ALLOW_REMOTE_HTTPS=1`` records that such a deployment was
    reviewed.

  Plain HTTP to any other host is always refused.

* The client never opens SSH connections or tunnels and never starts processes.
* Network failures are reported with whether the request may have reached
  Compute, so callers can tell "definitely not sent" from "outcome unknown".
"""

from __future__ import annotations

import http.client
import json
import re
import ssl
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from . import CLIENT_NAME, __version__
from .api_endpoints import ApiEndpoint, resolve
from .credentials import ApiToken
from .errors import NetworkTimeout, ServiceUnavailable, UnexpectedResponse, UsageError
from .transport import Origin, parse_origin

DEFAULT_API_URL = "http://127.0.0.1:18000"
API_URL_ENV = "BMD_RUN_API_URL"
ALLOW_REMOTE_HTTPS_ENV = "BMD_RUN_API_ALLOW_REMOTE_HTTPS"
DEFAULT_TIMEOUT_S = 300.0
MAX_REQUEST_BYTES = 3 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
LOOPBACK_ADDRESSES = ("127.0.0.1", "::1")

_UUID_PATH = re.compile(r"^/api/v1/attempts/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _allowed_request(method: str, path: str, has_body: bool) -> bool:
    """Literal second check, independent of the endpoint table."""

    if method == "POST":
        return path == "/api/v1/plans" and has_body
    if method == "PUT":
        return bool(_UUID_PATH.match(path)) and has_body
    if method == "GET":
        return bool(_UUID_PATH.match(path)) and not has_body
    return False


def check_secure_origin(origin: Origin, environ: Mapping[str, str]) -> None:
    """Refuse origins over which a bearer token must not travel."""

    if origin.scheme == "http":
        if origin.host in LOOPBACK_ADDRESSES and origin.port is not None:
            return
        raise UsageError(
            "Refusing to send API credentials over plain HTTP to a non-loopback address.",
            suggestion=(
                f"Use the SSH tunnel's loopback end ({DEFAULT_API_URL}). Host names such as "
                "'localhost' are not accepted; use 127.0.0.1."
            ),
        )
    if str(environ.get(ALLOW_REMOTE_HTTPS_ENV, "")).strip() == "1":
        return
    raise UsageError(
        "Refusing to send API credentials to an HTTPS origin that has not been reviewed.",
        suggestion=f"Use the SSH tunnel ({DEFAULT_API_URL}), or set {ALLOW_REMOTE_HTTPS_ENV}=1 for a reviewed HTTPS deployment.",
    )


@dataclass(frozen=True)
class ApiResponse:
    status: int
    document: Any


ConnectionFactory = Callable[[Origin, float], http.client.HTTPConnection]


def default_connection_factory(origin: Origin, timeout: float) -> http.client.HTTPConnection:
    if origin.scheme == "https":
        return http.client.HTTPSConnection(
            origin.host, origin.port, timeout=timeout, context=ssl.create_default_context()
        )
    return http.client.HTTPConnection(origin.host, origin.port, timeout=timeout)


class RequestOutcomeUnknown(Exception):
    """The request may have reached Compute but no complete response arrived."""

    def __init__(self, timed_out: bool) -> None:
        super().__init__("request outcome unknown")
        self.timed_out = timed_out


class ApiTransport:
    def __init__(
        self,
        base_url: str,
        token: ApiToken,
        *,
        environ: Mapping[str, str],
        timeout: float = DEFAULT_TIMEOUT_S,
        connection_factory: ConnectionFactory = default_connection_factory,
    ) -> None:
        self.origin = parse_origin(base_url)
        check_secure_origin(self.origin, environ)
        if not isinstance(token, ApiToken):
            raise UsageError("An API token is required.")
        if not (isinstance(timeout, (int, float)) and 0 < timeout <= 3600):
            raise UsageError("Timeout must be a number of seconds between 0 and 3600.")
        self._token = token
        self.timeout = float(timeout)
        self._connection_factory = connection_factory

    @property
    def base_url(self) -> str:
        return self.origin.url

    def call(self, endpoint: ApiEndpoint, *, attempt_id: Optional[str] = None, body: Optional[dict] = None) -> ApiResponse:
        """Send one request.

        Raises :class:`ServiceUnavailable` or :class:`NetworkTimeout` when the
        request certainly did not reach Compute (connection refused or connect
        timeout), and :class:`RequestOutcomeUnknown` when it may have.
        """

        route, path = resolve(endpoint, attempt_id)
        if (body is not None) != route.sends_body or not _allowed_request(route.method, path, body is not None):
            raise UsageError("Requests may only target members of the closed machine-API endpoint table.")
        payload = None
        headers = {
            "User-Agent": f"{CLIENT_NAME}/{__version__}",
            "Accept": "application/json",
            "Authorization": self._token.authorization_header(),
            "Connection": "close",
        }
        if body is not None:
            payload = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if len(payload) > MAX_REQUEST_BYTES:
                raise UsageError("The request is larger than BMD Compute accepts (3 MiB).")
            headers["Content-Type"] = "application/json"

        connection = None
        try:
            connection = self._connection_factory(self.origin, self.timeout)
            try:
                connection.connect()
            except TimeoutError:
                raise NetworkTimeout(
                    f"Timed out connecting to the BMD Compute API at {self.base_url}; nothing was sent.",
                    suggestion="Check that the SSH tunnel to the Compute host is running.",
                ) from None
            except (OSError, http.client.HTTPException):
                raise ServiceUnavailable(
                    f"Could not connect to the BMD Compute API at {self.base_url}; nothing was sent.",
                    suggestion="Check that the SSH tunnel to the Compute host is running.",
                ) from None
            try:
                connection.request(route.method, path, body=payload, headers=headers)
                raw = connection.getresponse()
                status = int(raw.status)
                content_type = raw.getheader("Content-Type", "") or ""
                data = raw.read(MAX_RESPONSE_BYTES + 1)
            except TimeoutError:
                raise RequestOutcomeUnknown(timed_out=True) from None
            except (OSError, http.client.HTTPException):
                raise RequestOutcomeUnknown(timed_out=False) from None
        finally:
            headers.pop("Authorization", None)
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

        if len(data) > MAX_RESPONSE_BYTES:
            raise UnexpectedResponse("The BMD Compute API response exceeded the size limit.", http_status=status)
        if 300 <= status < 400:
            raise UnexpectedResponse(
                "The BMD Compute API answered with a redirect; the client never follows redirects.",
                http_status=status,
            )
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise UnexpectedResponse("The BMD Compute API response is not JSON.", http_status=status)
        try:
            document = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise UnexpectedResponse("The BMD Compute API response is not valid JSON.", http_status=status) from None
        return ApiResponse(status=status, document=document)
