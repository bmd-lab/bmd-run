# bmd-run

A restricted, deterministic, user-level client for `bmd-compute`. It is developed by the Burton Materials Discovery Lab, Tel Aviv University, and is meant for
group members working in a terminal and for developer agents (Claude, Codex)
that need to exercise `bmd-compute` the way an ordinary student would.

`bmd-run` is the restricted user-level client for `bmd-compute`. "Client" describes
its architectural role, not its name: the product, repository, command,
distribution and import package are all `bmd-run` / `bmd_run`.

**Status:** the read/build commands are a proof of concept against frozen
`bmd-compute` v1.0.0. Milestone R1 added a separate, authenticated `bmd-run api`
capability for one calculation at a time through BMD Compute's machine API v1
(bmd-compute `e3fbb3b`), and Milestone R2 wraps it in one command. See
`ARCHITECTURE.md` for the design and its reasoning.

## Running a calculation

The recommended interface is one structure, one command, one BMD Compute calculation.
In a directory that contains a file named `POSCAR`:

```
bmd-run              # Energy only: a single-point PBE static on the structure as given
bmd-run --relax      # Relaxed structure
bmd-run --dos        # Electronic DOS
bmd-run --bands      # Electronic band structure
bmd-run --custom workflow.json
bmd-run --desired-output relaxed_energy   # Relaxed Energy (two relaxations, then a static)
```

Or name the structure file, with optional overrides:

```
bmd-run Si.vasp
bmd-run Bi2Se3.cif --bands --cpus 48 --memory-gb 128 --walltime 48:00:00
bmd-run Si.vasp --desired-output energy        # the R2 form, still supported
bmd-run Si.vasp --custom-workflow workflow.json
```

| Option | BMD Compute workflow |
| --- | --- |
| (none) | Desired Output `energy_only` |
| `--relax` | Desired Output `relaxed_structure` |
| `--dos` | Desired Output `electronic_dos` |
| `--bands` | Desired Output `electronic_band_structure` |
| `--custom FILE` | Custom workflow from FILE (same as `--custom-workflow FILE`) |
| `--desired-output ID` | any Desired Output identifier (for example `relaxed_energy` or `phonons`), or `energy`/`relax`/`dos`/`bands` |

With no option, `bmd-run` requests `energy_only`, BMD Compute's fixed-geometry
single-point static. This is bmd-run's own default and it is unchanged: since
bmd-compute E1 the Compute *browser* defaults to `relaxed_energy` (PBE geometry
optimisation, again, then an accurate PBE static) and no longer lists Energy only in
its menu, but `energy_only` keeps its meaning for the machine API, bmd-run and past
calculations. Ask for Relaxed Energy by its identifier, `--desired-output
relaxed_energy`; it has no shortcut or alias.

Only one workflow option may be given; combining any two (for example `--relax --dos`
or `--dos --desired-output energy`) is an error and nothing is sent.

**Which structure.** If a structure file is named, it is used. Otherwise `bmd-run`
uses the file named exactly `POSCAR` in the current directory, read as POSCAR. It
does not look anywhere else (no parent or sub-directories, no `CONTCAR`, `*.vasp` or
`*.cif`, no guessing), and it never modifies, renames or copies the file. If there
is no `./POSCAR`, it stops with an error before contacting BMD Compute. `--format`
applies only to a named file. A structure that BMD Compute rejects fails at the plan
step, before any attempt is recorded.

**Every invocation is a new calculation.** Running `bmd-run` twice in the same
directory submits two calculations with two attempts; `bmd-run` does not detect
repeats. To continue or check an earlier run, use its attempt ID with the `api`
commands below, never `bmd-run` again.

`bmd-run` asks BMD Compute for the authoritative plan, prints a short
summary, records the attempt locally, then has BMD Compute prepare it on POWER and
submit it to SLURM. It submits without asking for confirmation: that is what the
command is for. It prints the attempt ID, the SLURM job ID and how to check the job.

- **Structure:** a POSCAR or CIF file (`.cif` is read as CIF; `--format` overrides).
  The file's text is sent unchanged and the file is never modified.
- **Workflow:** one of the options in the table above. The values sent are BMD
  Compute's own identifiers; `--custom`/`--custom-workflow` take BMD Compute's Custom
  stage schema. With none, BMD Compute's Energy-only Desired Output is used, as in
  the browser interface. `bmd-run` infers no methodology.
- **Overrides:** `--cpus`, `--memory-gb`, `--walltime H:MM:SS` (up to four hour
  digits, e.g. `2400:00:00`) and `--queue` are passed to BMD Compute as given; BMD
  Compute validates them and fills in everything else. A resource you do not give
  is not sent, so a BMD Compute with Automatic Resource Selection chooses it; the
  plan summary then shows the resolved CPUs, memory, walltime and QOS and which of
  them were selected automatically (also in `--json`, under `resource_selection`).
  `bmd-run` adds no methodology, defaults or resource choices of its own.
- **Connection:** `--api-url`, `--token-file`, `--state-dir`, `--timeout` and `--json`
  work as for the `api` commands below (SSH tunnel at `http://127.0.0.1:18000`,
  protected token file).

**If it stops part-way.** Once the attempt is recorded, `bmd-run` always reports its
ID, how far it got and the recovery commands. Continue with the **same** attempt using
the `api` commands, which re-send exactly the recorded request (a changed structure
file or different options are never used):

```
bmd-run api prepare --attempt UUID   # if it stopped before preparation was confirmed
bmd-run api submit UUID              # submits that same attempt (never twice)
bmd-run api status UUID              # what BMD Compute reports now
```

A submission whose outcome is unknown (a lost response, a timeout, an unreadable
answer) is reported as **uncertain** (exit 15), never as failed, and is never retried
automatically: check it with `api status` or repeat `api submit` with the same ID.
Running `bmd-run` again starts a **new** calculation with a new attempt, so
do not use it to recover. If the process is killed before it can print the ID, the
attempt is still recorded as `<state dir>/attempts/<attempt id>.json` (default
`~/.local/state/bmd-run/attempts/`); with `--json` the ID is also written to stderr
as soon as it is recorded.

The `api` commands remain available for advanced, step-by-step use (plan only,
prepare without submitting) and for recovery. For several structures, see
`bmd-run batch` below. A structure file named like a command (`plan`, `api`, `submit`, ...) or like an option (`--dos`) must
be given as `./NAME`. `bmd-run --help` and `bmd-run --version` never start a
calculation; `bmd-run POSCAR --help` (or `bmd-run --relax --help`) shows the options
of the one-command interface.

## Running a campaign (`bmd-run batch`, Milestone R3A)

A campaign is a small, ordered list of single calculations in a JSON manifest. Each
one is run exactly as `bmd-run STRUCTURE` would run it (same request builder, same
plan, record, prepare and submit operations), and each is bound to exactly one
attempt that is reused whenever the batch is run again.

```
bmd-run batch benchmark.json               # submit every calculation in the manifest
bmd-run batch benchmark.json --max-new 2   # testing: start at most 2 new attempts this run
bmd-run batch status benchmark.json        # read-only report, refreshed from BMD Compute
bmd-run batch status benchmark.json --local   # report from the local records only
```

### Batch manifest

```json
{
  "version": 1,
  "campaign": "dft-benchmark-1",
  "calculations": [
    {"cell": "si-energy-24", "structure": "structures/Si.POSCAR", "desired_output": "energy_only"},
    {"cell": "si-energy-48", "structure": "structures/Si.POSCAR", "desired_output": "energy_only",
     "cpus": 48, "memory_gb": 64, "walltime": "12:00:00"},
    {"cell": "nio-dos", "structure": "structures/NiO.POSCAR", "desired_output": "dos"},
    {"cell": "si-custom", "structure": "structures/Si.cif", "custom_workflow": "relax_static.json"}
  ]
}
```

- `version` must be `1`; `campaign` and every `cell` are identifiers of 1-64
  letters, digits, `.`, `_` or `-` (starting with a letter or digit). Cell
  identifiers are unique within the manifest, ignoring case.
- `structure` (required) is a POSCAR or CIF file; `format` (`poscar` or `cif`) is
  optional as for `--format`. `custom_workflow` is a Custom workflow JSON file.
  Relative paths are resolved from the manifest's directory, not the current one.
- Exactly one of `desired_output` (an identifier or `energy`/`relax`/`dos`/`bands`)
  or `custom_workflow`. There is no default in a manifest.
- Optional `cpus` and `memory_gb` (positive JSON integers), `walltime`
  (`HH:MM:SS`) and `queue`, passed to BMD Compute unchanged. No other keys are
  accepted; BMD Compute resolves everything else.
- Any number of calculations (the manifest file may be up to 64 MiB). The whole
  manifest is read and every request is built before anything is sent; any problem
  stops the batch with nothing sent. Calculations are submitted one after another;
  SLURM schedules them.

### What a run does

For each calculation, in manifest order:

1. A cell that has never run is recorded locally first
   (`<state dir>/campaigns/<campaign>/<cell>.json`, private), with the attempt UUID
   it will use, before that UUID is sent anywhere. Every remaining cell is started,
   unless `--max-new N` (for testing) limits this run to N new attempts.
2. The attempt is then advanced with the R1 operations: plan, record the attempt,
   Prepare, Submit. The cell keeps the plan digest and the plan's structure
   summary, resolved workflow and resources.
3. A cell whose attempt is already submitted is left alone (nothing is sent). A
   cell whose submission is **uncertain** keeps its attempt and is never submitted
   again by the batch; check it with `bmd-run api status UUID` (shown in the output).

**Restarting is safe.** Running the same command again continues the campaign:
recorded cells reuse their attempt (an unprepared one is prepared, a prepared one is
submitted), and the remaining cells start. A recorded cell whose structure
file, workflow or resources have changed is refused, with nothing sent; give a
changed calculation a new cell identifier. New calculations may be appended to the
manifest at any time.

**One run per campaign.** While a `bmd-run batch` is advancing a campaign it holds
an exclusive lock on that campaign's state (`<state dir>/campaigns/<campaign>.lock`,
a POSIX `flock`). A second `bmd-run batch` for the same campaign, through any
manifest file, stops at once with exit code 21 and sends or records nothing. The lock is released when
the run ends, however it ends (including Ctrl-C or the process being killed).
Other campaigns run independently, and `batch status` takes no lock, so it can be
used while a campaign is running.

**When it stops.** A problem with one calculation (BMD Compute rejects its
structure or workflow, its plan changed, preparation or submission definitely
failed, or its submission outcome is uncertain) is recorded on that cell and the
batch moves on to the independent cells. Anything campaign-wide stops the batch at
that cell and leaves the rest untouched: a lost connection or timeout, a credential
or scope problem, a local state problem, Ctrl-C, or any other error not specific to
one cell. The exit code is that of the first error (see Exit codes); run the batch
again to continue. The client never retries on its own.

### Status

`bmd-run batch status MANIFEST` sends only `GET /api/v1/attempts/{uuid}` for
recorded attempts (it never plans, prepares or submits) and classifies each cell:

| Status | Meaning |
|---|---|
| `pending` | Not started, or not yet submitted (it will continue on the next run) |
| `submitted` | Submitted; the scheduler reports it queued or running |
| `completed` | The scheduler reports it finished successfully |
| `failed` | The job ended unsuccessfully, or BMD Compute rejected the calculation (needs a change) |
| `uncertain` | The submission outcome is unknown; check it with the commands shown |

The table and `--json` output carry what a benchmark needs per cell: campaign and
cell identifiers, attempt UUID, plan digest, structure (formula, atoms, space
group), resolved workflow (Desired Output and stages), nodes, CPUs, **requested**
memory, walltime limit, partition, SLURM job ID, and the scheduler's state,
elapsed time and exit code as reported by BMD Compute. Memory is the memory
requested for the job (`requested_memory_gb`); it is not measured peak memory, and
`bmd-run` does not measure memory use.

`--json` writes one `schema: "bmd_run.batch_output"`, `schema_version: 1` document
(`src/bmd_run/schemas/batch-output-v1.schema.json`) for both commands, with the
same rules as the other outputs: no Compute text, never the token.

## What it can do

| Command | What it does | Compute requests |
|---|---|---|
| `STRUCTURE` | Plan, record, prepare and submit one calculation (see above) | `POST /api/v1/plans`, `PUT /api/v1/attempts/{uuid}` (twice) |
| `batch MANIFEST` | The same, for each calculation of a campaign, reusing recorded attempts (see above) | the same, per cell |
| `batch status MANIFEST` | Read-only campaign report | `GET /api/v1/attempts/{uuid}` per recorded cell |
| `identity` | Behavioural fingerprint of the service. **Not** verified source identity. | `GET /openapi.json`, `POST /analyze`, `POST /build-workflow` (fixed probe) |
| `options` | Desired Outputs, stage types, theories and modifiers Compute offers | `POST /analyze` (fixed probe) |
| `analyze FILE` | Compute's structure summary, method considerations and default workflow | `POST /analyze` |
| `plan OUTPUT FILE` | Compute's resolved workflow and resources, then consideration status, structure facts, and per-stage INCAR/KPOINTS rebuilt by the client | `POST /build-workflow` |

The separate `api` commands (Milestone R1, see below) use BMD Compute's
authenticated machine API:

| Command | What it does | Compute requests | Token scope |
|---|---|---|---|
| `api plan FILE` | The authoritative plan and its digest. No side effects. | `POST /api/v1/plans` | `plan` |
| `api prepare FILE` | Plan, record a new attempt locally, prepare it on POWER (`submit=false`) | `POST /api/v1/plans`, `PUT /api/v1/attempts/{uuid}` | `plan`, `prepare` |
| `api prepare --attempt UUID` | Repeat Prepare for a recorded attempt, from its record only | `PUT /api/v1/attempts/{uuid}` | `prepare` |
| `api submit UUID` | Submit that same attempt (`submit=true`, nothing else changed) | `PUT /api/v1/attempts/{uuid}` | `submit` |
| `api status UUID` | The attempt's state, job ID and scheduler state | `GET /api/v1/attempts/{uuid}` | `read` |

## What it cannot do

It has no implementation of, and no hidden path to:

- SSH, SFTP or SLURM (BMD Compute alone connects to POWER and submits);
- creating SSH tunnels, running processes or remote commands;
- Monitor, Resume, cancellation or result retrieval;
- Materials Project retrieval, resource prediction or adaptive experimental design
  (a batch runs exactly the manifest it is given);
- Git or MongoDB;
- arbitrary HTTP.

The read/build commands cannot prepare or submit. The complete list of requests
the client can make is two closed tables. The frozen v1 table
(`src/bmd_run/endpoints.py`, unchanged):

```
GET  /openapi.json
POST /analyze
POST /build-workflow
```

and the authenticated machine-API table (`src/bmd_run/api_endpoints.py`):

```
POST /api/v1/plans
PUT  /api/v1/attempts/{canonical lowercase UUID}
GET  /api/v1/attempts/{canonical lowercase UUID}
```

It contains no scientific methodology and does no scientific validation.
`bmd-compute` decides what is valid. The client only checks the shape of its own
arguments: identifiers, file size, UTF-8 text, and resource values as short
tokens.

## Install and run

Standard library only, Python 3.10 or newer:

```
pip install .            # or: PYTHONPATH=src python -m bmd_run ...
export BMD_COMPUTE_URL=http://<compute-host>:<port>

bmd-run identity
bmd-run options
bmd-run analyze POSCAR
bmd-run plan energy POSCAR
bmd-run plan dos POSCAR --json
bmd-run plan relax structure.cif --cpus 48 --walltime 24:00:00
```

The `plan` aliases are spelling conveniences for Compute's own Desired Output
identifiers:

| Alias | Desired Output |
|---|---|
| `energy` | `energy_only` |
| `relax` | `relaxed_structure` |
| `dos` | `electronic_dos` |
| `bands` | `electronic_band_structure` |

Any other identifier (for example `relaxed_energy`) is sent as typed, and Compute
accepts or rejects it. Under
v1.0.0, `dos` resolves to **PBE geometry optimisation → HSE06 static → HSE06 DOS**,
which is why `plan` prints the resolved workflow and resources first.

## Authenticated machine API (`bmd-run api`, Milestone R1)

```
bmd-run on POWER -> HTTP over your existing SSH tunnel -> bmd-compute on the VM -> Compute-owned POWER execution
```

`bmd-run` stays a client. BMD Compute alone decides methodology, workflow
construction, automatic treatments, resources, remote preparation and SLURM
submission. `bmd-run` sends only what you give it: the structure file's text
(read, never modified), a Desired Output identifier or a Custom workflow in
Compute's stage schema, and optional `--cpus`, `--memory-gb`, `--walltime`,
`--queue`. It fills in no defaults and infers nothing.

```
# once: store the token BMD Compute issued you, readable only by you
mkdir -p ~/.config/bmd-run && install -m 600 /dev/null ~/.config/bmd-run/api-token
cat > ~/.config/bmd-run/api-token        # paste the token, then Ctrl-D

bmd-run api plan POSCAR --desired-output energy_only          # review the plan and its digest
bmd-run api prepare POSCAR --desired-output energy_only \
        --expect-plan-digest sha256:...                         # optional: bind to the reviewed plan
bmd-run api submit 6f1d2c3b-...                                 # the attempt ID prepare printed
bmd-run api status 6f1d2c3b-...
bmd-run api plan structure.cif --custom-workflow stages.json  # {"stages": [{"stage_type": ..., "theory": ...}]}
```

**Transport and credentials.** The default API origin is `http://127.0.0.1:18000`,
the POWER end of the existing SSH tunnel (`--api-url` or `BMD_RUN_API_URL` to
change it). Credentials are sent only to `http://127.0.0.1:PORT` or
`http://[::1]:PORT`; host names such as `localhost` and every other plain-HTTP
host are refused, and HTTPS is refused unless `BMD_RUN_API_ALLOW_REMOTE_HTTPS=1`
records a reviewed deployment (certificates are always verified). The token is
read from `--token-file`, else `BMD_RUN_API_TOKEN_FILE`, else
`~/.config/bmd-run/api-token` (a regular file owned by you, mode 600), or from
`BMD_RUN_API_TOKEN`. It goes only into the `Authorization` header: never into
URLs, output, logs or attempt records. `bmd-run` never opens SSH connections,
tunnels or processes itself.

**Attempts and recovery.** `api prepare` asks Compute for the plan, records its
digest, chooses a UUID, and writes the attempt record (mode 600, under
`--state-dir`, `BMD_RUN_STATE_DIR`, `$XDG_STATE_HOME/bmd-run` or
`~/.local/state/bmd-run`, directories mode 700) **before** sending Prepare. The
record holds the attempt UUID, the Compute API origin, the exact request and its
SHA-256, the expected plan digest and the locally observed state; never
credentials, SSH settings, remote paths or submission identity tokens.

- `api submit` and `api prepare --attempt` re-send the recorded request; they take
  no structure, workflow or resource arguments, so changed CLI defaults can never
  change a resumed request. A record whose request no longer matches its stored
  SHA-256 (accidental corruption, a partial edit) is refused. This is a
  consistency check, not tamper protection: a record deliberately edited and
  rehashed by its owner is not guaranteed to be detected, and deliberate local
  modification is outside the threat model. Once Compute has registered an
  attempt UUID, Compute's binding of that UUID to its request is authoritative
  and a changed request is refused (exit 14).
- After a timeout or lost response, repeat the same command with the same
  attempt ID, or run `api status`. A new UUID is never generated automatically.
- An uncertain submission (exit 15) is never answered by another attempt.
  BMD Compute never submits one attempt twice.
- `api status UUID` also reads attempts created by other clients under the same
  Compute principal, without a local record (nothing is written for them).
  Compute's optional `campaign`/`cell` labels are validated against Compute's
  label rule; only the label names are shown, never their values.

## `--json` output (for synthetic-user testing)

`bmd-run [STRUCTURE] --json` writes one `schema: "bmd_run.run_output"`,
`schema_version: 1` document (`src/bmd_run/schemas/run-output-v1.schema.json`), on
success and on error. It reports `stage` (`plan`, `prepare`, `submit` or `complete`),
`attempt_id` once the attempt is recorded, the `recovery` commands, and in `result`
the plan and attempt objects of `bmd_run.machine_output` v1, under the same rules.


Every command accepts `--json`, before or after the command name. It then
writes exactly one JSON document to stdout, on success and on error, conforming
to `src/bmd_run/schemas/output-v3.schema.json`
(`schema: "bmd_run.output"`, `schema_version: 3`).

Consumers should:

- check `schema` and `schema_version` first;
- then check `ok`;
- then read `result` (on success) or `error` (on failure).

`compute.adapter_status` is `"transitional"` while the client reads Compute
v1's HTML pages.

Every object in the schema is closed (`additionalProperties: false`), and
vocabulary fields are `enum`s. Schema versions 1 and 2 relayed Compute text.
They were never released and are superseded.

The `api` commands write `schema: "bmd_run.machine_output"`, `schema_version: 1`
(`src/bmd_run/schemas/machine-output-v1.schema.json`) with the same rules: no
Compute prose (error messages, suggestions, option text, module names, the
space-group symbol) is relayed; vocabulary values are the client's copies of a
closed vocabulary pinned to bmd-compute `e3fbb3b` (`api_vocabulary.py`);
digests, job IDs and timestamps are re-rendered after strict pattern checks.

## No Compute text reaches output

Compute's responses are untrusted, and the client relays none of their text.
Pattern-based secret screening was tried and abandoned: split or re-encoded
secrets passed through free-text fields. Instead, the client never relays
Compute-originated text at all. Every string in a result, and every word of
the human-readable output, is one of:

1. a client constant;
2. the client's own copy of a member of a closed vocabulary pinned to BMD
   Compute v1.0.0 (`v1_vocabulary.py`): Desired Outputs, stage types,
   theories, modifiers, VASP executables, partition/account, consideration ids
   and their exact v1 titles, failure titles, crystal systems, element symbols;
3. rendered by the client from numbers parsed within bounds (at most 12
   significant digits), for example lattice parameters, resources and INCAR
   values.

What each command reports:

- **Workflow:** Desired Output and per-stage type, theory and modifiers.
  Stage names are rebuilt by the client from those values.
- **Resources:** typed values.
- **Structure facts:** formula rebuilt from element symbols and counts, atom
  count, volume, density, lattice, space-group number, crystal system.
- **Method considerations:** id and a status
  (`applied` / `not_applied_automatically` / `advisory` / `unrecognized`),
  recognised from v1's exact titles. The prose is not read.
- **Generated inputs:** INCAR and KPOINTS for every stage, rebuilt from
  validated tokens (`v1_inputs.py`).
  - INCAR tags come from a closed tag vocabulary generated from pymatgen's
    `incar_parameters.json` (`incar_tags.py`, made by
    `tools/generate_incar_tags.py`). Values are parsed by syntactic type, and
    word values must be from that tag's closed list.
  - Unknown and free-text tags (e.g. `SYSTEM`) are withheld: counted, never shown.
  - KPOINTS automatic meshes are shown in full. Explicit lists are reported as
    a count only.
- **Compute rejections:** a stage category (`structure_validation`,
  `calculation_validation`, `unrecognized`) and fixed client text.

Fields v1 offers only as free text are listed in each result under
`unavailable_from_v1_machine_interface`:

- consideration and error prose;
- display names and calculation-plan text;
- the space-group symbol;
- comments;
- explicit k-point coordinates;
- the POSCAR preview;
- scripts and paths.

They return with `/api/v1`.

**Residual channel (accepted, documented):** a deliberately malicious Compute
could encode data in the numbers it emits. Each number is bounded and
re-rendered by the client, so this is a low-bandwidth numeric covert channel,
not a text relay. Closing it entirely would mean reporting no Compute-derived
numbers.

## Closed endpoint table

`Endpoint` members are opaque identifiers with no route attributes, and
assigning to them raises an error. Routes are immutable `NamedTuple`s in a
read-only mapping read only by `endpoints.resolve()`. The transport then
re-checks the resolved method and path against a literal allowlist before
sending anything.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Compute rejected the request (category in `error.compute_stage`; Compute's prose is not relayed) |
| 2 | Usage error; nothing was sent |
| 3 | Compute unreachable |
| 4 | Unexpected response / contract mismatch |
| 5 | `identity --strict` fingerprint mismatch |
| 10 | `api`: authentication failed (token not accepted) |
| 11 | `api`: the token lacks the scope this command needs |
| 12 | `api`: invalid structure, workflow, resources or request fields |
| 13 | `api`: plan digest mismatch (the plan changed; nothing was prepared or submitted) |
| 14 | `api`: attempt conflict (bound to another request, principal or origin) |
| 15 | `api`: submission uncertain; check with `api status`, never create a new attempt |
| 16 | `api`: quota exceeded |
| 17 | `api`: service unavailable (tunnel down, Compute busy or not configured) |
| 18 | `api`: network timeout |
| 19 | `api`: no usable API token |
| 20 | `api`: attempt not found on Compute |
| 21 | `api`: local attempt state missing, unsafe, damaged or inconsistent |
| 22 | `api`: remote preparation or submission failed on POWER |

## Tests

```
python -m unittest discover -s tests
```

The suite includes:

- the static security checks;
- endpoint-mutation regressions;
- the regressions for both Codex findings: the whole token in
  `workflow_spec_json`, and the split-token and split-UUID attacks;
- an adversarial matrix that injects the page's real token and attempt UUID,
  in 11 encodings, into every Compute field the adapter still reads and into
  fields it ignores. The encodings are plain, split with spaces or newlines,
  UUID groups, HTML entities, percent-encoded, reversed, hex, base32,
  interleaved with client words, and zero-width-joined.
- an installed-wheel smoke test that builds from a clean source copy, installs
  into a fresh environment, checks `bmd-run --help` / `--version`, imports
  `bmd_run`, and inspects wheel contents;
- for `bmd-run api`: a fake BMD Compute served over real HTTP on 127.0.0.1 from
  responses recorded from bmd-compute `e3fbb3b` with Compute's own in-memory fake
  POWER (`tests/fixtures/compute_api_v1/`, `tools/record_machine_api_fixtures.py`).
  It covers POSCAR, CIF, Desired Output and Custom requests, scopes, the endpoint
  allowlist, persistence before Prepare, Prepare -> Submit, repeated and
  interrupted requests, changed-request rejection, malformed responses and token
  redaction, plus an information-flow matrix over every recorded response field.

The matrix checks an information-flow property rather than looking for
secrets. Every alphabetic word in JSON and human output must be a word owned
by the client package (numbers and client-computed SHA-256 digests excepted),
and no payload fragment may appear.

Optional live run against a clean local Compute v1.0.0 (Analyze/Build only, no
remote side effects):

```
BMD_RUN_LIVE_URL=http://127.0.0.1:8765 \
  python -m unittest discover -s tests -p "test_live*.py"
```

## Fixture provenance

`tests/fixtures/compute_v1.0.0/` holds verbatim responses from a clean local
Compute v1.0.0. `tools/fixtures_v1.py` re-captures them through the client's
own closed transport, with Compute used only as an external HTTP service, and
compares them with the committed files. Only four documented per-request values
are normalised (identity token, UUIDs, run timestamps, exact-script SHA-256).
See `tests/fixtures/compute_v1.0.0/RECORDING.md`.

## Transitional adapter

Compute v1.0.0 has no machine-facing API, so `v1_html.py` reads the browser
template from commit `a746155`. It is intentionally minimal and will be replaced
by Compute's planned JSON `/api/v1` facade.

## License and security

`bmd-run` is released under the MIT License. See `LICENSE`.

Security-sensitive reports should be made privately first. See `SECURITY.md`.
Small contributions that preserve the client boundary are welcome; see
`CONTRIBUTING.md`.
