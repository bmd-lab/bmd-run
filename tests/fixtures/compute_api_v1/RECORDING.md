# BMD Compute machine API v1 fixtures

Recorded from bmd-compute `e3fbb3beaf5c0084023df9fcdd76deea4edc4778` (main after
PR #44, the machine execution API) with `tools/record_machine_api_fixtures.py`.

## How they were recorded

* The recorder runs inside a bmd-compute checkout's own test environment and
  calls Compute's FastAPI application in-process (Starlette `TestClient`, client
  address `127.0.0.1`). No network connection is opened.
* Every remote operation goes to Compute's in-memory fake POWER from its own test
  suite (`tests/test_machine_api_execution.py::FakePower`): no SSH, no scheduler,
  no POWER. Job IDs (`920001`, ...) come from that fake.
* Tokens are generated for the run, stored only in a temporary token store and
  never recorded; the recorder refuses to write a file containing token material.
* Structures: `Si.POSCAR` and `NiO.POSCAR` are the v1 fixtures; `Si.cif` is
  pymatgen's `CifWriter` output for `Si.POSCAR`.
* Attempt bodies are exactly what bmd-run sends: the plan request plus
  `expected_plan_digest` and `submit`, with no labels.

Each `<case>.json` holds the request (method, path, principal, body) and
Compute's response (status, content type, JSON body).

## Normalisations

Only these, everything else is Compute's JSON re-serialised with sorted keys:

* M1: UTC timestamps (`created_at`, `checked_at`) become `2026-10-10T00:00:00Z`;
* M2: the local submission time (`submitted_at_local`) becomes `2026-10-10 00:00:00`.

## Verifying

`MANIFEST.json` lists the SHA-256 of every file; `tests/test_machine_fixtures.py`
checks it offline. To re-record and compare against a Compute checkout:

    python tools/record_machine_api_fixtures.py compare --compute-checkout PATH

(run with that checkout's Python environment). `compare` reported no differences
when these fixtures were committed.
