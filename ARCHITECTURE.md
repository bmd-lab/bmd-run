# bmd-run Architecture

`bmd-run` is a restricted command-line client for `bmd-compute`. It is designed for
materials-science users who need deterministic, scriptable access to Compute's
read/build workflow without gaining submission or HPC execution authority.

The central boundary is simple:

- `bmd-run` is a client adapter.
- `bmd-compute` is the scientific methodology and execution authority.
- `bmd-run` does not implement VASP methodology, SSH, SFTP, SLURM, database
  access, submission, monitoring or result collection.

## Scope

`bmd-run` v0.1 targets frozen `bmd-compute` v1.0.0. It supports four user-facing
operations:

| Command | Purpose | Compute surface |
|---|---|---|
| `bmd-run identity` | Behavioural fingerprint of the configured Compute service | `GET /openapi.json`, `POST /analyze`, `POST /build-workflow` |
| `bmd-run options` | Desired Outputs and option vocabulary exposed by Compute | `POST /analyze` |
| `bmd-run analyze` | Structure summary and Compute-resolved default workflow | `POST /analyze` |
| `bmd-run plan` | Compute-resolved workflow preview for a Desired Output | `POST /build-workflow` |

The transport table is closed over exactly these HTTP operations:

- `GET /openapi.json`
- `POST /analyze`
- `POST /build-workflow`

No other HTTP method or path is part of `bmd-run`'s authority.

## Explicit Non-Goals

`bmd-run` must not grow hidden authority. In particular it must not provide:

- Prepare, Submit, Monitor or Resume;
- arbitrary HTTP paths or methods;
- SSH, SFTP, SLURM or subprocess execution;
- Git, MongoDB or other database access;
- arbitrary filesystem writes;
- arbitrary INCAR, KPOINTS, POTCAR or scientific-methodology editing;
- access to Compute submission tokens, attempt UUIDs, monitor state or job IDs.

The client may display Compute-resolved provenance in safe, bounded output. That
currently includes the resolved POWER partition and account, plus bounded CPU,
node, memory and walltime resource information returned by `bmd-compute`. Those
values are provenance, not client-owned policy.

## Information Flow

`bmd-compute` returns HTML. `bmd-run` treats that HTML as untrusted input and projects
it into a stable, versioned output model. Human text and JSON output are built
from client-owned vocabulary plus bounded values parsed from known Compute v1
structures.

The client intentionally discards browser-only submission state such as hidden
identity tokens, attempt IDs, monitor state, job IDs and full submission specs.
Those fields belong to `bmd-compute`'s execution lifecycle and are outside BMD
Run's read/build scope.

`identity` is a behavioural fingerprint. It can show that a service behaves like
the frozen `bmd-compute` v1.0.0 reference on the client's probes. It is not a
cryptographic or deployed-source attestation.

## Compute And POWER

`bmd-compute` owns execution. On current deployments it may resolve workflows to
POWER resources, SLURM partitions and accounts. `bmd-run` reports the resolved
partition, account, CPU/node count, memory and walltime when they are part of
the Build preview, but it cannot use them to execute anything itself.

Execution paths, shared paths, loaded modules, runtime environments and
submission scripts are deliberately unavailable through the frozen v1
machine-facing adapter and are not reported by `bmd-run`.

This distinction is intentional. Public `bmd-run` output may include exact
Compute-resolved resource provenance, while `bmd-run` still remains incapable of
connecting to POWER, preparing a run, submitting a job or monitoring a job.

## Ecosystem Naming

The current BMD ecosystem names are:

- `bmd-compute`: scientific methodology and execution service;
- `bmd-run`: restricted read/build client;
- `bmd-check`: observational diagnostics;
- `bmd-store`: curated scientific and contextual data;
- `bmd-help`: documentation.

The Python package for this project is `bmd_run`, the distribution name is
`bmd-run`, and the console command is `bmd-run`.

## Test Strategy

`bmd-run`'s tests focus on the client boundary:

- the endpoint table is exactly the three allowed Compute operations;
- outgoing form fields do not contain submission, monitor, job or backend state;
- proxy, redirect and URL handling cannot move transport off the configured
  Compute origin;
- malformed Compute HTML cannot smuggle arbitrary text, tokens or UUIDs into
  human or JSON output;
- the package imports only a small standard-library allowlist and contains no
  subprocess, SSH, Git, database or filesystem-write capability;
- recorded Compute v1.0.0 fixtures can be verified against a clean Compute
  checkout;
- the built wheel installs into an isolated environment and exposes only the
  `bmd-run` command and `bmd_run` package.

These tests prove what `bmd-run` can do. They do not replace access control,
authentication, resource policy or execution safeguards in `bmd-compute`.
