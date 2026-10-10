"""Loading the machine-API bearer token.

Two sources are supported, and nothing else:

* ``BMD_RUN_API_TOKEN``: the token itself, in the environment;
* a protected token file: ``--token-file``, else ``BMD_RUN_API_TOKEN_FILE``,
  else ``~/.config/bmd-run/api-token``.

Giving both the environment token and an explicit file is refused as ambiguous.

The file must be a regular file (not a symbolic link) owned by the current
user, with no group or other permission bits, and at most 4 KiB. It is opened
without following links and re-checked on the open descriptor. It must contain
exactly one token of BMD Compute's form ``bmdc1.<token_id>.<secret>``.

The token is held in :class:`ApiToken`, whose ``repr`` and ``str`` are
redacted. It is only ever placed in the HTTP authorisation header by the
authenticated transport. It is never printed, logged, persisted or put in a
URL. Error messages never contain file contents.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Mapping, Optional

from .errors import CredentialsUnavailable, UsageError

TOKEN_ENV = "BMD_RUN_API_TOKEN"
TOKEN_FILE_ENV = "BMD_RUN_API_TOKEN_FILE"
MAX_TOKEN_FILE_BYTES = 4096
# BMD Compute compute_api/auth.py: bmdc1.<8-32 lowercase alphanumerics>.<43 URL-safe base64 characters>
_TOKEN = re.compile(r"^bmdc1\.[a-z0-9]{8,32}\.[A-Za-z0-9_-]{43}$")


def default_token_file() -> Path:
    return Path.home() / ".config" / "bmd-run" / "api-token"


class ApiToken:
    """An opaque bearer token. Its text never appears in ``repr``/``str``."""

    __slots__ = ("_value", "source")

    def __init__(self, value: str, source: str) -> None:
        if not isinstance(value, str) or not _TOKEN.match(value):
            raise CredentialsUnavailable(
                f"The API token from {source} is not a BMD Compute machine-API token.",
                suggestion="Tokens look like bmdc1.<id>.<secret>; ask the BMD Compute administrator for one.",
            )
        self._value = value
        self.source = source

    def authorization_header(self) -> str:
        return "Bearer " + self._value

    def secret_fragments(self) -> tuple:
        """Substrings that must never appear in any output (used by the redaction guard)."""

        return (self._value, self._value.rsplit(".", 1)[1])

    def __repr__(self) -> str:
        return f"ApiToken(<redacted>, source={self.source!r})"

    __str__ = __repr__

    def __reduce__(self):  # never pickled
        raise TypeError("ApiToken cannot be serialised.")


def load_token(token_file: Optional[str], environ: Mapping[str, str]) -> ApiToken:
    from_env = environ.get(TOKEN_ENV)
    if from_env is not None and from_env.strip():
        if token_file:
            raise UsageError(
                f"Both {TOKEN_ENV} and --token-file are set; use one token source.",
            )
        return ApiToken(from_env.strip(), f"${TOKEN_ENV}")
    path_text = token_file or environ.get(TOKEN_FILE_ENV) or str(default_token_file())
    return ApiToken(_read_protected_file(Path(path_text).expanduser()), "the token file")


def _read_protected_file(path: Path) -> str:
    if os.name != "posix":
        raise CredentialsUnavailable(
            "Token files are only supported on POSIX systems, where their permissions can be checked.",
            suggestion=f"Set {TOKEN_ENV} for this session instead.",
        )
    missing = CredentialsUnavailable(
        "No API token is available.",
        suggestion=(
            f"Put the token in {default_token_file()} (mode 600), pass --token-file, "
            f"or set {TOKEN_ENV}."
        ),
    )
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        raise missing from None
    except OSError:
        raise CredentialsUnavailable("The token file could not be inspected.") from None
    _check_protected(before)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise CredentialsUnavailable("The token file could not be opened.") from None
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise CredentialsUnavailable("The token file changed while it was being read.")
        _check_protected(opened)
        data = os.read(descriptor, MAX_TOKEN_FILE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > MAX_TOKEN_FILE_BYTES:
        raise CredentialsUnavailable("The token file is too large to be a token file.")
    try:
        text = data.decode("ascii").strip()
    except UnicodeDecodeError:
        raise CredentialsUnavailable("The token file does not contain a BMD Compute machine-API token.") from None
    if not _TOKEN.match(text):
        raise CredentialsUnavailable(
            "The token file does not contain a BMD Compute machine-API token.",
            suggestion="It must hold exactly one token of the form bmdc1.<id>.<secret>.",
        )
    return text


def _check_protected(info) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise CredentialsUnavailable("The token file must be a regular file, not a link or directory.")
    if info.st_uid != os.getuid():
        raise CredentialsUnavailable("The token file must be owned by the current user.")
    if info.st_mode & 0o077:
        raise CredentialsUnavailable(
            "The token file is readable or writable by other users.",
            suggestion="Run: chmod 600 <token file>",
        )
    if info.st_size > MAX_TOKEN_FILE_BYTES:
        raise CredentialsUnavailable("The token file is too large to be a token file.")
