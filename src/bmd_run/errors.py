"""Client error types and their stable process exit codes."""

from __future__ import annotations

from typing import Optional

EXIT_OK = 0
EXIT_COMPUTE_REJECTED = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3
EXIT_UNEXPECTED_RESPONSE = 4
EXIT_FINGERPRINT_MISMATCH = 5
# Machine API (``bmd-run api ...``) outcomes.
EXIT_AUTHENTICATION_FAILED = 10
EXIT_INSUFFICIENT_SCOPE = 11
EXIT_INVALID_REQUEST = 12
EXIT_PLAN_DIGEST_MISMATCH = 13
EXIT_ATTEMPT_CONFLICT = 14
EXIT_SUBMISSION_UNCERTAIN = 15
EXIT_QUOTA_EXCEEDED = 16
EXIT_SERVICE_UNAVAILABLE = 17
EXIT_NETWORK_TIMEOUT = 18
EXIT_CREDENTIALS_UNAVAILABLE = 19
EXIT_ATTEMPT_NOT_FOUND = 20
EXIT_LOCAL_STATE = 21
EXIT_REMOTE_OPERATION_FAILED = 22


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


# --- machine API (``bmd-run api ...``) ----------------------------------------------
# Messages and suggestions are client-owned text. ``compute_code`` is the
# client's own copy of a recognised Compute error code (``api_vocabulary``).


class ApiError(ClientError):
    """Base class for machine-API outcomes."""

    def __init__(self, message: str, *, compute_code: Optional[str] = None, details: Optional[dict] = None, **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.compute_code = compute_code
        self.details = dict(details or {})


class AuthenticationFailed(ApiError):
    kind = "authentication_failed"
    exit_code = EXIT_AUTHENTICATION_FAILED


class InsufficientScope(ApiError):
    kind = "insufficient_scope"
    exit_code = EXIT_INSUFFICIENT_SCOPE


class InvalidRequest(ApiError):
    """Compute rejected the structure, workflow or resources (nothing was executed)."""

    kind = "invalid_request"
    exit_code = EXIT_INVALID_REQUEST


class PlanDigestMismatch(ApiError):
    kind = "plan_digest_mismatch"
    exit_code = EXIT_PLAN_DIGEST_MISMATCH


class AttemptConflict(ApiError):
    kind = "attempt_conflict"
    exit_code = EXIT_ATTEMPT_CONFLICT


class SubmissionUncertain(ApiError):
    """The submission outcome is unknown. Never answered by creating another attempt."""

    kind = "submission_uncertain"
    exit_code = EXIT_SUBMISSION_UNCERTAIN


class QuotaExceeded(ApiError):
    kind = "quota_exceeded"
    exit_code = EXIT_QUOTA_EXCEEDED


class ServiceUnavailable(ApiError):
    kind = "service_unavailable"
    exit_code = EXIT_SERVICE_UNAVAILABLE


class NetworkTimeout(ApiError):
    kind = "network_timeout"
    exit_code = EXIT_NETWORK_TIMEOUT


class AttemptNotFound(ApiError):
    kind = "attempt_not_found"
    exit_code = EXIT_ATTEMPT_NOT_FOUND


class RemoteOperationFailed(ApiError):
    kind = "remote_operation_failed"
    exit_code = EXIT_REMOTE_OPERATION_FAILED


class CredentialsUnavailable(ClientError):
    """No usable API token. Messages never contain token material."""

    kind = "credentials_unavailable"
    exit_code = EXIT_CREDENTIALS_UNAVAILABLE


class LocalStateError(ClientError):
    """The local attempt store is missing, unsafe or inconsistent."""

    kind = "local_state_error"
    exit_code = EXIT_LOCAL_STATE
