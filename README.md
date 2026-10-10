# bmd-run

A restricted, deterministic, user-level client for `bmd-compute`. It is developed by the Burton Materials Discovery Lab, Tel Aviv University, and is meant for
group members working in a terminal and for developer agents (Claude, Codex)
that need to exercise `bmd-compute` the way an ordinary student would.

`bmd-run` is the restricted user-level client for `bmd-compute`. "Client" describes
its architectural role, not its name: the product, repository, command,
distribution and import package are all `bmd-run` / `bmd_run`.

**Status:** the read/build commands are a proof of concept against frozen
`bmd-compute` v1.0.0. Milestone R1 adds a separate, authenticated `bmd-run api`
capability for one calculation at a time through BMD Compute's machine API v1
(bmd-compute `e3fbb3b`). See `ARCHITECTURE.md` for the design and its reasoning.

## What it can do

| Command | What it does | Compute requests |
|---|---|---|
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
- Materials Project retrieval, batch manifests or campaigns;
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

Any other identifier is sent as typed, and Compute accepts or rejects it. Under
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

## `--json` output (for synthetic-user testing)

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
