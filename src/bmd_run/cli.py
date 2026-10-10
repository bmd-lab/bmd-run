"""Command-line interface.

* ``bmd-run [STRUCTURE] [--relax | --dos | --bands | --custom FILE] [OPTIONS]``: the
  recommended one-command interface (without STRUCTURE it uses ./POSCAR). It runs one
  calculation through BMD Compute's machine API by calling the R1 operations in order
  (``machine.run``: plan, record the attempt, prepare, submit). See ``_main_run``.
* ``bmd-run {identity,options,analyze,plan}``: read/build-only commands against
  the frozen BMD Compute v1.0.0 browser interface (unchanged from v0.1.0).
* ``bmd-run api {plan,prepare,submit,status}``: the separate, authenticated
  machine-API capability (BMD Compute machine API v1), reached through the
  user's existing SSH tunnel. See ``machine.py``.

Validation here is limited to the shape of CLI arguments and requests. Whether
a Desired Output, structure or resource value is acceptable is decided by
BMD Compute.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable, List, Mapping, Optional

from . import __version__, machine, machine_output, operations, output
from .api_transport import API_URL_ENV, DEFAULT_API_URL, ApiTransport
from .api_transport import DEFAULT_TIMEOUT_S as API_DEFAULT_TIMEOUT_S
from .attempt_store import AttemptStore, default_state_dir
from .credentials import load_token
from .errors import (
    EXIT_FINGERPRINT_MISMATCH,
    EXIT_OK,
    ClientError,
    UsageError,
)
from .transport import DEFAULT_TIMEOUT_S, Transport

COMPUTE_URL_ENV = "BMD_COMPUTE_URL"
MAX_STRUCTURE_BYTES = 2 * 1024 * 1024
MAX_CUSTOM_WORKFLOW_BYTES = 64 * 1024
API_COMMANDS = ("plan", "prepare", "submit", "status")
# The first positional argument selects a command if it is one of these names; any other
# first positional argument is a structure file for ``bmd-run STRUCTURE``. Words that are,
# or might be mistaken for, commands are reserved too, so that for example ``bmd-run submit
# X`` stays a usage error and never starts a calculation. (A structure file with one of these
# names can be given as ./NAME.)
COMMAND_NAMES = ("identity", "options", "analyze", "plan", "api")
RESERVED_WORDS = ("prepare", "submit", "status", "monitor", "resume", "cancel", "run", "batch", "help")
DEFAULT_DESIRED_OUTPUT = "energy_only"  # Compute's Energy-only Desired Output, as in the UI
# Desired Output shortcuts of the one-command interface: BMD Compute's own identifiers
# (api_vocabulary.DESIRED_OUTPUTS), named as in the Compute UI. No option means DEFAULT_DESIRED_OUTPUT.
DESIRED_OUTPUT_SHORTCUTS = {
    "--relax": "relaxed_structure",
    "--dos": "electronic_dos",
    "--bands": "electronic_band_structure",
}
AUTODETECTED_STRUCTURE = "POSCAR"  # used, as POSCAR, when no structure file is given
_OPTIONS_WITH_VALUES = frozenset({
    "--compute-url", "--timeout", "--format", "--desired-output", "--custom-workflow", "--custom", "--cpus",
    "--memory-gb", "--walltime", "--queue", "--api-url", "--token-file", "--state-dir",
})
# With no positional argument these keep the existing behaviour (help, version, or the
# read/build commands' usage error) instead of running ./POSCAR ...
_NO_RUN_WITHOUT_STRUCTURE = ("-h", "--help", "--version", "--compute-url")
# ... except that help asked for together with a one-command workflow option is the
# one-command help (argparse prints it while parsing, before ./POSCAR is looked for).
_RUN_HELP_OPTIONS = tuple(DESIRED_OUTPUT_SHORTCUTS) + ("--custom",)

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
    parser.add_argument(
        "--timeout", type=float, default=default(None),
        help=f"Seconds per request (default {DEFAULT_TIMEOUT_S:g}; {API_DEFAULT_TIMEOUT_S:g} for 'api' commands).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=default(False),
        help="Emit one versioned JSON document (bmd_run.output v3; bmd_run.machine_output v1 for 'api').",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="bmd-run",
        description=(
            "Restricted client for BMD Compute. To run one calculation: "
            "'bmd-run [STRUCTURE] [--relax | --dos | --bands | --custom FILE] [resources]' "
            "(without STRUCTURE, ./POSCAR is used; see 'bmd-run STRUCTURE --help'). "
            "BMD Compute plans, prepares and submits it. "
            "The 'api' commands are the same machine-API operations one at a time, for advanced "
            "use and recovery. The identity, options, analyze and plan commands are read/build-only "
            "against BMD Compute v1.0.0 and cannot prepare or submit."
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

    _add_api_commands(commands, common)
    return parser


def _add_api_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--api-url", default=None,
        help=f"Machine-API origin (default: ${API_URL_ENV}, else {DEFAULT_API_URL}, the SSH tunnel's loopback end).",
    )
    parser.add_argument("--token-file", default=None, help="Protected file holding the API token (mode 600).")
    parser.add_argument("--state-dir", default=None, help="Protected directory for local attempt records.")


def _add_scientific_request(parser: argparse.ArgumentParser, *, required: bool) -> None:
    parser.add_argument("structure_file", nargs=None if required else "?", help="POSCAR or CIF file (read only).")
    parser.add_argument("--format", choices=_FORMATS, default=None, help="Default: from the file name.")
    parser.add_argument("--desired-output", default=None, help="BMD Compute Desired Output id (or alias).")
    parser.add_argument("--custom-workflow", default=None, help="JSON file in BMD Compute's Custom stage schema.")
    parser.add_argument("--cpus", default=None)
    parser.add_argument("--memory-gb", dest="memory_gb", default=None)
    parser.add_argument("--walltime", default=None, help="HH:MM:SS")
    parser.add_argument("--queue", default=None)


def _add_api_commands(commands, common: argparse.ArgumentParser) -> None:
    api = commands.add_parser(
        "api", parents=[common],
        help="Authenticated machine-API commands (plan, prepare, submit, status).",
        description=(
            "BMD Compute machine API v1 over the SSH tunnel. BMD Compute owns methodology, "
            "resources, preparation and submission; bmd-run only sends your request."
        ),
    )
    _add_api_options(api)
    actions = api.add_subparsers(dest="api_command", metavar="API_COMMAND", parser_class=_ArgumentParser)
    shared = _ArgumentParser(add_help=False)
    _add_common_options(shared, defaults=False)
    for name in ("--api-url", "--token-file", "--state-dir"):
        shared.add_argument(name, dest=name[2:].replace("-", "_"), default=argparse.SUPPRESS)

    plan = actions.add_parser("plan", parents=[shared], help="Request the authoritative plan (no side effects).")
    _add_scientific_request(plan, required=True)

    prepare = actions.add_parser(
        "prepare", parents=[shared],
        help="Plan, record a new attempt, and prepare it on POWER without submitting.",
    )
    _add_scientific_request(prepare, required=False)
    prepare.add_argument("--expect-plan-digest", default=None,
                         help="Refuse unless BMD Compute still resolves exactly this plan digest.")
    prepare.add_argument("--attempt", default=None,
                         help="Repeat Prepare for this recorded attempt (from its record only).")

    submit = actions.add_parser("submit", parents=[shared], help="Submit a recorded, prepared attempt.")
    submit.add_argument("attempt_id")

    status = actions.add_parser("status", parents=[shared], help="Read an attempt's state from BMD Compute.")
    status.add_argument("attempt_id")


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
    api_transport_factory: Callable[..., ApiTransport] = ApiTransport,
    environ: Optional[Mapping[str, str]] = None,
    stdout=None,
    stderr=None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    environ = os.environ if environ is None else environ
    first = _first_positional(argv)
    if first is None:
        names = {item.split("=", 1)[0] for item in argv}
        if not names & set(_NO_RUN_WITHOUT_STRUCTURE) or (
                names & {"-h", "--help"} and names & set(_RUN_HELP_OPTIONS) and "--version" not in names):
            return _main_run(argv, api_transport_factory, environ, stdout, stderr)  # ./POSCAR, or its help
    elif argv[first] not in COMMAND_NAMES + RESERVED_WORDS:
        return _main_run(argv, api_transport_factory, environ, stdout, stderr)
    if "api" in argv[: _first_command_index(argv) + 1]:
        return _main_api(argv, api_transport_factory, environ, stdout, stderr)
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
                suggestion="Use one of: identity, options, analyze, plan, api.",
            )
        base_url = args.compute_url or environ.get(COMPUTE_URL_ENV)
        timeout = DEFAULT_TIMEOUT_S if args.timeout is None else args.timeout
        transport = transport_factory(base_url, timeout=timeout)
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


def _first_command_index(argv: List[str]) -> int:
    """Index of the first positional argument (the command), skipping top-level options."""

    takes_value = {"--compute-url", "--timeout"}
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in takes_value:
            index += 2
            continue
        if item.startswith("-"):
            index += 1
            continue
        return index
    return len(argv)


def _first_positional(argv: List[str]) -> Optional[int]:
    """Index of the first positional argument, skipping every option and its value."""

    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--":
            return index + 1 if index + 1 < len(argv) else None
        if item in _OPTIONS_WITH_VALUES:
            index += 2
            continue
        if item.startswith("-"):
            index += 1
            continue
        return index
    return None


def build_run_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="bmd-run",
        usage="bmd-run [STRUCTURE] [--relax | --dos | --bands | --custom FILE | --desired-output ID] [options]",
        description=(
            "Run one calculation on POWER through BMD Compute: request the plan, record the attempt "
            "locally, prepare it and submit it, without prompting. BMD Compute decides the methodology, "
            "inputs and resources; bmd-run only sends your structure, workflow choice and overrides. "
            "Without STRUCTURE, the file POSCAR in the current directory is used. Without a workflow "
            "option, BMD Compute's Energy-only Desired Output is used. Each invocation is a new "
            "calculation. Recover or inspect an attempt with 'bmd-run api prepare --attempt UUID', "
            "'bmd-run api submit UUID' and 'bmd-run api status UUID'."
        ),
    )
    parser.add_argument("structure_file", metavar="STRUCTURE", nargs="?", default=None,
                        help=f"POSCAR or CIF file (read only). Default: ./{AUTODETECTED_STRUCTURE}.")
    parser.add_argument("--format", choices=_FORMATS, default=None,
                        help="For an explicit STRUCTURE: default from the file name (.cif is CIF).")
    workflow = parser.add_mutually_exclusive_group()
    for flag, identifier in DESIRED_OUTPUT_SHORTCUTS.items():
        workflow.add_argument(flag, dest="shortcut", action="store_const", const=identifier, default=None,
                              help=f"BMD Compute Desired Output {identifier}.")
    workflow.add_argument("--custom", dest="custom_shortcut", metavar="FILE", default=None,
                          help="Custom workflow JSON file (same as --custom-workflow).")
    workflow.add_argument(
        "--desired-output", default=None,
        help="BMD Compute Desired Output id, or alias: " + ", ".join(
            f"{alias}={value}" for alias, value in DESIRED_OUTPUT_ALIASES.items()
        ) + f" (default: {DEFAULT_DESIRED_OUTPUT}).",
    )
    workflow.add_argument("--custom-workflow", default=None, help="JSON file in BMD Compute's Custom stage schema.")
    parser.add_argument("--cpus", default=None)
    parser.add_argument("--memory-gb", dest="memory_gb", default=None)
    parser.add_argument("--walltime", default=None, help="HH:MM:SS")
    parser.add_argument("--queue", default=None)
    _add_api_options(parser)
    parser.add_argument("--timeout", type=float, default=None,
                        help=f"Seconds per request (default {API_DEFAULT_TIMEOUT_S:g}).")
    parser.add_argument("--json", action="store_true", default=False,
                        help="Emit one versioned JSON document (bmd_run.run_output v1).")
    return parser


def _main_run(argv, api_transport_factory, environ, stdout, stderr) -> int:
    """``bmd-run STRUCTURE``: the R1 operations in order, via ``machine.run``."""

    want_json = "--json" in argv
    api_url = None
    request = None
    fragments: tuple = ()
    progress: dict = {"stage": "plan", "attempt_id": None}
    structure_name = None

    def emit(text: str) -> None:
        stdout.write(_redact(text, fragments) + "\n")
        stdout.flush()

    def on_event(event, value):
        if want_json:
            if event == "persisted":  # so the attempt is findable even if this process is killed
                stderr.write(f"bmd-run: attempt {value} recorded\n")
            return
        text = machine_output.render_run_event(event, value, structure_name)
        if text is not None:
            emit(text)

    try:
        args = build_run_parser().parse_args(argv)
        want_json = args.json
        _select_structure(args)
        structure_name = Path(args.structure_file).name
        if args.shortcut is not None:
            args.desired_output = args.shortcut
        if args.custom_shortcut is not None:
            args.custom_workflow = args.custom_shortcut
        if args.desired_output is None and args.custom_workflow is None:
            args.desired_output = DEFAULT_DESIRED_OUTPUT
        api_url = args.api_url or environ.get(API_URL_ENV) or DEFAULT_API_URL
        timeout = API_DEFAULT_TIMEOUT_S if args.timeout is None else args.timeout
        scientific_request, source = _api_request(args)
        request = {**machine.request_summary(scientific_request), "structure_source": source}
        token = load_token(args.token_file, environ)
        fragments = token.secret_fragments()
        api = api_transport_factory(api_url, token, environ=environ, timeout=timeout)
        api_url = api.base_url
        store = AttemptStore(Path(args.state_dir).expanduser() if args.state_dir else default_state_dir(environ))
        progress = machine.run(api, store, scientific_request, source, on_event=on_event)
    except ClientError as error:
        return _run_failed(error, progress, want_json, api_url, request, fragments, stdout, stderr)
    except machine.RunStopped as stopped:
        progress = stopped.progress
        if isinstance(stopped.error, KeyboardInterrupt):
            error = UsageError(
                "Interrupted. The outcome of the request in progress is unknown.",
                suggestion="Check the attempt before doing anything else.",
            )
            _run_failed(error, progress, want_json, api_url, request, fragments, stdout, stderr)
            return 130
        return _run_failed(stopped.error, progress, want_json, api_url, request, fragments, stdout, stderr)

    if want_json:
        stdout.write(_redact(machine_output.to_json(machine_output.run_envelope(
            api_url=api_url, request=request, progress=progress, error=None)), fragments))
    else:
        emit(machine_output.render_run_footer(progress))
    return EXIT_OK


def _select_structure(args) -> None:
    """Use ./POSCAR (as POSCAR) when no structure file is given; never search further."""

    if args.structure_file is not None:
        return
    if args.format is not None:
        raise UsageError("--format applies only to an explicit structure file; ./POSCAR is always read as POSCAR.")
    if not Path(AUTODETECTED_STRUCTURE).is_file():
        raise UsageError(
            f"No structure file was given and there is no {AUTODETECTED_STRUCTURE} file in the current directory.",
            suggestion=f"Run 'bmd-run STRUCTURE_FILE', or run 'bmd-run' in a directory that contains {AUTODETECTED_STRUCTURE}.",
        )
    args.structure_file = AUTODETECTED_STRUCTURE
    args.format = "poscar"


def _run_failed(error, progress, want_json, api_url, request, fragments, stdout, stderr) -> int:
    stage, attempt_id = progress.get("stage", "plan"), progress.get("attempt_id")
    error.suggestion = machine_output.run_suggestion(error, stage, attempt_id)
    if want_json:
        stdout.write(_redact(machine_output.to_json(machine_output.run_envelope(
            api_url=api_url, request=request, progress=progress, error=error)), fragments))
    else:
        stderr.write(_redact(machine_output.render_run_error(error, progress), fragments) + "\n")
    return error.exit_code


def _read_custom_workflow(path_text: str) -> dict:
    path = Path(path_text)
    if not path.is_file():
        raise UsageError(f"Custom workflow file not found: {path_text}")
    if path.stat().st_size > MAX_CUSTOM_WORKFLOW_BYTES:
        raise UsageError("The Custom workflow file is too large.")
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise UsageError("The Custom workflow file is not valid UTF-8 JSON.") from None
    return machine.custom_workflow_shape(document)


def _api_request(args) -> tuple:
    """Build the scientific request from CLI arguments: returns (request, structure_source)."""

    if args.structure_file is None:
        raise UsageError("A structure file is required.", suggestion="bmd-run api prepare STRUCTURE --desired-output ID")
    if (args.desired_output is None) == (args.custom_workflow is None):
        raise UsageError("Give exactly one of --desired-output or --custom-workflow.")
    text, fmt = read_structure(args.structure_file, args.format)
    desired_output = resolve_desired_output(args.desired_output) if args.desired_output is not None else None
    custom = _read_custom_workflow(args.custom_workflow) if args.custom_workflow is not None else None
    resources = machine.resources_request(cpus=args.cpus, memory_gb=args.memory_gb, walltime=args.walltime, queue=args.queue)
    request = machine.plan_request(structure_text=text, structure_format=fmt, desired_output=desired_output,
                                   custom_workflow=custom, resources=resources)
    source = {
        "file_name": Path(args.structure_file).name,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "format": fmt,
    }
    return request, source


def _redact(text: str, fragments) -> str:
    for fragment in fragments:
        if fragment:
            text = text.replace(fragment, "<redacted>")
    return text


def _main_api(argv, api_transport_factory, environ, stdout, stderr) -> int:
    want_json = "--json" in argv
    command = None
    api_url = None
    request = None
    fragments: tuple = ()
    try:
        args = build_parser().parse_args(argv)
        want_json = args.json
        command = getattr(args, "api_command", None)
        if command is None:
            raise UsageError("No api command given.", suggestion="Use one of: " + ", ".join(API_COMMANDS) + ".")
        if args.compute_url is not None:
            raise UsageError("--compute-url applies to the read/build commands; use --api-url for 'api' commands.")
        if command == "prepare":
            scientific = [args.structure_file, args.desired_output, args.custom_workflow, args.format, args.cpus,
                          args.memory_gb, args.walltime, args.queue, args.expect_plan_digest]
            if args.attempt is not None and any(item is not None for item in scientific):
                raise UsageError(
                    "--attempt repeats a recorded attempt exactly; it takes no structure, workflow or resources.",
                )
        api_url = args.api_url or environ.get(API_URL_ENV) or DEFAULT_API_URL
        timeout = API_DEFAULT_TIMEOUT_S if args.timeout is None else args.timeout

        scientific_request = source = None
        if command in ("plan", "prepare") and not (command == "prepare" and args.attempt is not None):
            scientific_request, source = _api_request(args)
            request = {**machine.request_summary(scientific_request), "structure_source": source}
        elif command == "prepare":
            request = {"attempt_id": args.attempt}
        else:
            request = {"attempt_id": args.attempt_id}

        token = load_token(args.token_file, environ)
        fragments = token.secret_fragments()
        api = api_transport_factory(api_url, token, environ=environ, timeout=timeout)
        api_url = api.base_url
        store = AttemptStore(Path(args.state_dir).expanduser() if args.state_dir else default_state_dir(environ))

        if command == "plan":
            result = machine.plan(api, scientific_request)
        elif command == "prepare" and args.attempt is not None:
            result = machine.prepare_resume(api, store, args.attempt)
        elif command == "prepare":
            expected = args.expect_plan_digest
            if expected is not None and not re.match(r"^sha256:[0-9a-f]{64}$", expected):
                raise UsageError("--expect-plan-digest must look like sha256:<64 hex digits>.")
            result = machine.prepare_new(api, store, scientific_request, source, expected_plan_digest=expected)
        elif command == "submit":
            result = machine.submit(api, store, args.attempt_id)
        else:
            result = machine.status(api, store, args.attempt_id)
    except ClientError as error:
        if want_json:
            text = machine_output.to_json(machine_output.envelope(
                command=command, api_url=api_url, request=request, result=None, error=error))
            stdout.write(_redact(text, fragments))
        else:
            stderr.write(_redact(machine_output.render_error(error), fragments) + "\n")
        return error.exit_code

    if want_json:
        text = machine_output.to_json(machine_output.envelope(
            command=command, api_url=api_url, request=request, result=result, error=None))
        stdout.write(_redact(text, fragments))
    else:
        stdout.write(_redact(machine_output.render_result(command, result), fragments) + "\n")
    return EXIT_OK


def entry_point() -> None:  # pragma: no cover
    raise SystemExit(main())
