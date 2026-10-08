"""Client error types and their stable process exit codes."""

from __future__ import annotations

from typing import Optional

EXIT_OK = 0
EXIT_COMPUTE_REJECTED = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3
EXIT_UNEXPECTED_RESPONSE = 4
EXIT_FINGERPRINT_MISMATCH = 5


class ClientError(Exception):
    """Base class for every error the client reports."""

    kind = "client_error"
    exit_code = EXIT_UNEXPECTED_RESPONSE

    def __init__(
        self,
        message: str,
        *,
        suggestion: Optional[str] = None,
        http_status: Optional[int] = None,
        compute_stage: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.suggestion = suggestion
        self.http_status = http_status
        self.compute_stage = compute_stage

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "message": self.message,
            "suggestion": self.suggestion,
            "http_status": self.http_status,
            "compute_stage": self.compute_stage,
        }


class UsageError(ClientError):
    """The CLI arguments or request shape are invalid. Nothing was sent."""

    kind = "usage_error"
    exit_code = EXIT_USAGE


class ComputeUnreachable(ClientError):
    """BMD Compute could not be reached (network, VPN, TLS, timeout)."""

    kind = "compute_unreachable"
    exit_code = EXIT_UNREACHABLE


class ComputeRejected(ClientError):
    """BMD Compute rejected the request.

    The message and suggestion are client-owned generic text chosen by the
    rejection category (``compute_stage``). Compute's own reason prose is
    never relayed.
    """

    kind = "compute_rejected"
    exit_code = EXIT_COMPUTE_REJECTED


class UnexpectedResponse(ClientError):
    """The response did not match the frozen v1.0.0 contract the client expects."""

    kind = "unexpected_response"
    exit_code = EXIT_UNEXPECTED_RESPONSE
