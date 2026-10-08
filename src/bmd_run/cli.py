"""Command-line interface: ``bmd-run {identity,options,analyze,plan}``.

Validation here is limited to the shape of CLI arguments and requests. Whether
a Desired Output, structure or resource value is acceptable is decided by
BMD Compute.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Callable, List, Optional

from . import __version__, operations, output
from .errors import (
    EXIT_FINGERPRINT_MISMATCH,
    EXIT_OK,
    ClientError,
    UsageError,
)
from .transport import DEFAULT_TIMEOUT_S, Transport

COMPUTE_URL_ENV = "BMD_COMPUTE_URL"
MAX_STRUCTURE_BYTES = 2 * 1024 * 1024

# Convenience spellings only. The values are BMD Compute's own Desired Output
# identifiers and are passed through unchanged; any other identifier is sent
# as typed and BMD Compute decides whether it exists.
DESIRED_OUTPUT_ALIASES = {
    "energy": "energy_only",
    "relax": "relaxed_structure",
    "dos": "electronic_dos",
    "bands": "electronic_band_structure",
}

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RESOURCE_VALUE = re.compile(r"^[A-Za-z0-9:._-]{1,32}$")
_FORMATS = ("poscar", "cif")


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):  # pragma: no cover - exercised through main()
        raise UsageError(message, suggestion=f"Run '{self.prog} --help'.")


def _add_common_options(parser: argparse.ArgumentParser, *, defaults: bool) -> None:
    def default(value):
        return value if defaults else argparse.SUPPRESS

    parser.add_argument(
        "--compute-url",
        default=default(None),
        help=f"BMD Compute origin, e.g. http://host:8000 (default: ${COMPUTE_URL_ENV}).",
    )
    parser.add_argument("--timeout", type=float, default=default(DEFAULT_TIMEOUT_S), help="Seconds per request.")
    parser.add_argument(
        "--json",
        action="store_true",
        default=default(False),
        help="Emit the versioned JSON output document (schema bmd_run.output v3).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="bmd-run",
        description=(
            "Restricted read/build-only client for BMD Compute v1.0.0. "
            "It can fingerprint, list options, analyze and plan. It cannot prepare or submit."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _add_common_options(parser, defaults=True)

    # The same options are accepted after the command name as well.
    common = _ArgumentParser(add_help=False)
    _add_common_options(common, defaults=False)

    commands = parser.add_subparsers(dest="command", metavar="COMMAND", parser_class=_ArgumentParser)

    identity = commands.add_parser("identity", parents=[common], help="Behavioural fingerprint of the Compute service.")
    identity.add_argument(
        "--strict",
        action="store_true",
        help=f"Exit {EXIT_FINGERPRINT_MISMATCH} if the fingerprint differs from the v1.0.0 reference.",
    )

    commands.add_parser("options", parents=[common], help="Desired Outputs and options offered by Compute.")

    analyze = commands.add_parser("analyze", parents=[common], help="Analyze a structure file.")
    analyze.add_argument("structure_file")
    analyze.add_argument("--format", choices=_FORMATS, default=None, help="Default: from file name.")

    plan = commands.add_parser("plan", parents=[common], help="Build/preview a Desired Output (cannot prepare or submit).")
    plan.add_argument(
        "desired_output",
        help="Compute Desired Output id, or alias: " + ", ".join(
            f"{alias}={value}" for alias, value in DESIRED_OUTPUT_ALIASES.items()
        ),
    )
    plan.add_argument("structure_file")
    plan.add_argument("--format", choices=_FORMATS, default=None, help="Default: from file name.")
    plan.add_argument("--cpus", default=None)
    plan.add_argument("--memory-gb", dest="memory_gb", default=None)
    plan.add_argument("--walltime", default=None)
    plan.add_argument("--queue", default=None)
    return parser


def read_structure(path_text: str, fmt: Optional[str]) -> tuple:
    path = Path(path_text)
    if not path.is_file():
        raise UsageError(f"Structure file not found: {path_text}")
    size = path.stat().st_size
    if size == 0:
        raise UsageError(f"Structure file is empty: {path_text}")
    if size > MAX_STRUCTURE_BYTES:
        raise UsageError(f"Structure file is larger than {MAX_STRUCTURE_BYTES} bytes: {path_text}")
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UsageError(f"Structure file is not UTF-8 text: {path_text}") from exc
    if fmt is None:
        fmt = "cif" if path.suffix.lower() == ".cif" else "poscar"
    return text, fmt


def resolve_desired_output(value: str) -> str:
    resolved = DESIRED_OUTPUT_ALIASES.get(value, value)
    if not _IDENTIFIER.match(resolved):
        raise UsageError(
            f"Desired Output must be an identifier such as energy_only, not {value!r}.",
            suggestion="Run 'bmd-run options' to list what BMD Compute offers.",
        )
    return resolved


def plan_resources(args) -> dict:
    resources = {}
    for name in ("cpus", "memory_gb", "walltime", "queue"):
        value = getattr(args, name)
        if value is None:
            continue
        if not _RESOURCE_VALUE.match(value):
            raise UsageError(f"--{name.replace('_', '-')} has an invalid shape: {value!r}.")
        resources[name] = value
    return resources


def _structure_request(text: str, fmt: str, path_text: str) -> dict:
    return {
        "structure_file": path_text,
        "format": fmt,
        "structure_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def main(
    argv: Optional[List[str]] = None,
    *,
    transport_factory: Callable[..., Transport] = Transport,
    stdout=None,
    stderr=None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    want_json = "--json" in argv
    command = None
    base_url = None
    request = None
    warnings: List[str] = []

    try:
        args = build_parser().parse_args(argv)
        want_json = args.json
        command = args.command
        if command is None:
            raise UsageError(
                "No command given.",
                suggestion="Use one of: identity, options, analyze, plan.",
            )
        base_url = args.compute_url or os.environ.get(COMPUTE_URL_ENV)
        transport = transport_factory(base_url, timeout=args.timeout)
        base_url = transport.base_url

        exit_code = EXIT_OK
        if command == "identity":
            request = {}
            result = operations.identity(transport)
            if not result["matches_reference"]:
                warnings.append("Behavioural fingerprint differs from the BMD Compute v1.0.0 reference.")
                if args.strict:
                    exit_code = EXIT_FINGERPRINT_MISMATCH
        elif command == "options":
            request = {}
            result = operations.options(transport)
        elif command == "analyze":
            text, fmt = read_structure(args.structure_file, args.format)
            request = _structure_request(text, fmt, args.structure_file)
            result = operations.analyze(transport, text, fmt)
        else:  # plan
            desired_output = resolve_desired_output(args.desired_output)
            resources = plan_resources(args)
            text, fmt = read_structure(args.structure_file, args.format)
            request = {
                **_structure_request(text, fmt, args.structure_file),
                "desired_output": desired_output,
                "desired_output_as_typed": args.desired_output,
                "resources": resources,
            }
            result = operations.plan(transport, text, fmt, desired_output, resources)
    except ClientError as error:
        if want_json:
            stdout.write(output.to_json(output.envelope(
                command=command, base_url=base_url, request=request,
                result=None, error=error, warnings=warnings,
            )))
        else:
            stderr.write(output.render_error(error) + "\n")
        return error.exit_code

    if want_json:
        stdout.write(output.to_json(output.envelope(
            command=command, base_url=base_url, request=request,
            result=result, error=None, warnings=warnings,
        )))
    else:
        stdout.write(output.RENDERERS[command](result) + "\n")
        for warning in warnings:
            stderr.write(f"warning: {warning}\n")
    return exit_code


def entry_point() -> None:  # pragma: no cover
    raise SystemExit(main())
