"""BMD Run: a restricted, deterministic user-level client for BMD Compute.

This proof of concept talks to a frozen BMD Compute v1.0.0 deployment through a
closed table of exactly three HTTP endpoints (see ``endpoints.py``) and exposes
four read/build-only operations: ``identity``, ``options``, ``analyze`` and
``plan``.

The client contains no scientific methodology. BMD Compute decides what is
valid; the client only checks the shape of its own CLI arguments and requests.
"""

__all__ = ["__version__", "CLIENT_NAME"]

# Keep equal to [project] version in pyproject.toml (tests/test_version_identity.py).
# Releases v0.1.0 and v0.2.0 both reported "0.1.0"; this is the first release whose
# reported version matches its tag (v0.3.0).
__version__ = "0.3.0"
CLIENT_NAME = "bmd-run"
