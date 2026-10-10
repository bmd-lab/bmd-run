# bmd-run Architecture

`bmd-run` is a restricted command-line client for `bmd-compute`. It is designed for
materials-science users who need deterministic, scriptable access to Compute's
read/build workflow and, since Milestone R1, to Compute's authenticated
single-calculation execution, without gaining HPC execution authority itself.

The central boundary is simple:

- `bmd-run` is a client adapter.
- `bmd-compute` is the scientific methodology and execution authority. It alone
  constructs workflows, validates resources, prepares runs on POWER and submits
  them to SLURM.
- `bmd-run` does not implement VASP methodology, SSH, SFTP, SLURM, database
  access, monitoring or result collection. It asks Compute to prepare and
  submit through Compute's machine API, and nothing else.

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

Milestone R1 adds a second, separately authenticated table (`api_endpoints.py`)
for BMD Compute's machine API v1 (bmd-compute `e3fbb3b`, `docs/machine_api.md`):

| Command | Purpose | Compute surface | Scope |
|---|---|---|---|
| `bmd-run api plan` | The authoritative plan and plan digest | `POST /api/v1/plans` | `plan` |
| `bmd-run api prepare` | Bind a new attempt to that plan and prepare it (`submit=false`) | `POST /api/v1/plans`, `PUT /api/v1/attempts/{uuid}` | `plan`, `prepare` |
| `bmd-run api submit` | Submit the same recorded attempt (`submit=true`) | `PUT /api/v1/attempts/{uuid}` | `submit` |
| `bmd-run api status` | The attempt's authoritative state | `GET /api/v1/attempts/{uuid}` | `read` |

Milestone R2 adds the recommended one-command form, `bmd-run [STRUCTURE] [options]`,
which performs `api prepare` followed by `api submit` for the same new attempt. It
uses no other routes or scopes (`plan`, `prepare`, `submit`).

No other HTTP method or path is part of `bmd-run`'s authority. The attempt path
is built only from a validated canonical lowercase UUID, no query string is ever
sent, and each transport re-checks requests against its own literal allowlist.

## Explicit Non-Goals

`bmd-run` must not grow hidden authority. In particular it must not provide:

- any route to the frozen v1 browser Prepare, Submit, Monitor or Resume forms;
- Monitor, Resume, cancellation or result retrieval by any route;
- arbitrary HTTP paths or methods;
- SSH, SFTP, SLURM, tunnels or subprocess execution;
- Git, MongoDB or other database access;
- filesystem writes other than its own private attempt records;
- arbitrary INCAR, KPOINTS, POTCAR or scientific-methodology editing, or any
  local inference of automatic treatments;
- access to Compute submission identity tokens, attempt fingerprints, SSH
  profiles, remote paths or SLURM scripts;
- Materials Project retrieval, batch manifests or campaign scheduling.

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

## Machine API Capability (Milestone R1)

**Transport.** `api_transport.py` is the only module that sends a credential.
It speaks JSON over `http.client` to `http://127.0.0.1:18000` by default, the
POWER end of the user's existing SSH tunnel to the Compute VM. Bearer tokens
are sent only to literal loopback addresses over HTTP, or over verified HTTPS
when `BMD_RUN_API_ALLOW_REMOTE_HTTPS=1` records a reviewed deployment. Proxies,
redirects and query strings are never used, and responses are size-limited.
Network failures are classified by whether the request may have reached
Compute: a refused connection means nothing was sent; a timeout or lost
response after sending means the outcome is unknown.

**Credentials.** `credentials.py` reads one token from `BMD_RUN_API_TOKEN` or a
protected file (regular, owned by the user, mode 600, not a link, re-checked on
the open descriptor). The token object is redacted in `repr`, and output is
also passed through a redaction guard. Records never contain it.

**Planning-only versus execution scope.** `api plan` makes only the side-effect
free planning request. `api prepare` sends `submit=false` and needs `prepare`;
only `api submit` sends `submit=true` and needs `submit`; `api status` needs
`read`. Compute enforces the scopes; the client never escalates a request.

**Attempt binding and persistence.** `attempt_store.py` is the only module that
writes files. Before Prepare, `bmd-run` obtains the plan, records its digest,
creates a UUID and persists the attempt record (closed schema: UUID, Compute
API origin, the exact request and its SHA-256, expected plan digest, local
state and bounded event history) with an exclusive create. Submit and resumed
Prepare re-send that record's request, with only `submit` changed, and refuse
a record for a different API origin or one whose request no longer matches its
stored SHA-256. That SHA-256 detects accidental corruption and inconsistent
edits; it is not tamper protection. Deliberate modification of a record by the
account that owns it is outside the threat model (university-managed accounts,
POSIX permissions and protected credentials are the local boundary), and once
Compute has registered an attempt UUID, Compute's immutable binding of that UUID
to its request is authoritative. Timeouts are never answered by a
new UUID, and an uncertain submission never leads to another attempt; Compute's
own ledger and remote locks guarantee at most one `sbatch` per attempt.

**Output.** As for the v1 adapter, Compute's JSON is untrusted. Results are
projected into `bmd_run.machine_output` v1 from a closed vocabulary pinned to
bmd-compute `e3fbb3b` (`api_vocabulary.py`), bounded numbers, and
pattern-checked digests, job IDs and timestamps. Compute's error messages,
suggestions, option prose, module names and the space-group symbol are not
relayed; errors carry client text and the recognised Compute code.

## One Command (Milestone R2)

`bmd-run STRUCTURE` is a thin sequence over the R1 operations, not a second
execution path. `machine.run` calls `machine.prepare_new` (plan, record the attempt
with an exclusive create, Prepare with `submit=false`) and then `machine.submit` for
the same attempt ID (the recorded request with `submit=true`). It adds no request
building, state, retries or error classification: R1's errors propagate unchanged,
wrapped only with how far the run got and the attempt ID, so the CLI can print the
R1 recovery commands (`api prepare --attempt`, `api submit`, `api status`).
Consequences:

- a failed plan records nothing; once recorded, an attempt is never replaced, and a
  failure or uncertainty in Prepare stops the run before any submission;
- an uncertain submission keeps R1's classification (exit 15) and is never retried;
  running `bmd-run` again deliberately starts a new attempt;
- the default workflow (Compute's `energy_only` Desired Output) and the shortcut
  aliases are the only client-side choices, and they are Compute identifiers.

Dispatch is by the first positional argument: the command names `identity`,
`options`, `analyze`, `plan` and `api`, and reserved words such as `prepare`,
`submit`, `status`, `run` and `batch`, select the existing commands (or their usage
errors); anything else is a structure file.

### POSCAR autodetection and shortcuts (R2.1)

R2.1 changes only argument handling in `cli.py`; `machine.run`, the transports and
the attempt store are unchanged. With no positional argument, `bmd-run` uses the
file named exactly `POSCAR` in the current directory, read as POSCAR, and checks it
exists before anything is sent; it never searches elsewhere and never writes next to
the file. An explicit structure file always wins, and `--format` is accepted only
with one. `--relax`, `--dos` and `--bands` set the Desired Output to Compute's
`relaxed_structure`, `electronic_dos` and `electronic_band_structure`; `--custom FILE`
is `--custom-workflow FILE`. All workflow options are one argparse mutually
exclusive group, so any combination is a usage error. Without a positional
argument, `--help`, `-h`, `--version` and the read/build-only `--compute-url` keep
their previous meaning (help, version, or the read/build usage error), so they never
start a calculation; help together with a shortcut or `--custom` shows the
one-command help. There is no duplicate detection: each invocation is a new attempt.

## Compute And POWER

`bmd-compute` owns execution. On current deployments it may resolve workflows to
POWER resources, SLURM partitions and accounts. `bmd-run` reports the resolved
partition, account, CPU/node count, memory and walltime when they are part of
the Build preview, but it cannot use them to execute anything itself.

Execution paths, shared paths, loaded modules, runtime environments and
submission scripts are deliberately unavailable through the frozen v1
machine-facing adapter and are not reported by `bmd-run`.

The machine API reports the same resource provenance (partition, account,
nodes, CPUs, memory, walltime), the attempt's state, its SLURM job ID and a
bounded scheduler summary. Remote paths, modules, runtime environments and
submission scripts remain unavailable.

This distinction is intentional. Public `bmd-run` output may include exact
Compute-resolved resource provenance, while `bmd-run` itself remains incapable of
connecting to POWER. It can only ask BMD Compute, over the authenticated machine
API, to prepare or submit one calculation, and BMD Compute decides.

## Ecosystem Naming

The current BMD ecosystem names are:

- `bmd-compute`: scientific methodology and execution service;
- `bmd-run`: restricted client (read/build, and authenticated single-calculation execution through `bmd-compute`);
- `bmd-check`: observational diagnostics;
- `bmd-store`: curated scientific and contextual data;
- `bmd-help`: documentation.

The Python package for this project is `bmd_run`, the distribution name is
`bmd-run`, and the console command is `bmd-run`.

## Test Strategy

`bmd-run`'s tests focus on the client boundary:

- each endpoint table is exactly its three allowed Compute operations, and the
  tables are disjoint;
- only the token loader and API transport handle credentials, only the attempt
  store writes files, attempt IDs are generated in one place and only the
  submit operation sets `submit=true`;
- a fake BMD Compute over real HTTP, serving responses recorded from
  bmd-compute `e3fbb3b` with Compute's in-memory fake POWER, exercises plan,
  prepare, submit and status, persistence before Prepare, interrupted and
  repeated requests, changed-request rejection, scope failures and malformed
  responses;
- outgoing form fields do not contain submission, monitor, job or backend state;
- proxy, redirect and URL handling cannot move transport off the configured
  Compute origin;
- malformed Compute HTML cannot smuggle arbitrary text, tokens or UUIDs into
  human or JSON output;
- the package imports only a small standard-library allowlist and contains no
  subprocess, SSH, Git or database capability, and no filesystem writes outside
  the attempt store;
- recorded Compute v1.0.0 fixtures can be verified against a clean Compute
  checkout;
- the built wheel installs into an isolated environment and exposes only the
  `bmd-run` command and `bmd_run` package.

These tests prove what `bmd-run` can do. They do not replace access control,
authentication, resource policy or execution safeguards in `bmd-compute`.
