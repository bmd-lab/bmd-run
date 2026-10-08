# Recorded BMD Compute v1.0.0 responses

These files are verbatim HTTP response bodies, first recorded on 2026-10-02
from a clean, unmodified checkout of BMD Compute:

- release `v1.0.0`, commit `a746155b487f903a167eca6b3c92e860aaf8c7f5`
- started with `uvicorn main:app --host 127.0.0.1 --port 8765`
- Python 3.13.15. Scientific runtime installed from the repository's
  `constraints/scientific-runtime.txt` (atomate2 0.1.5, pymatgen 2026.5.4,
  pymatgen-core 2026.7.16, custodian 2025.12.14, emmet-core 0.87.1,
  jobflow 0.1.19, spglib 2.7.0). FastAPI 0.142.2, Starlette 1.7.0,
  Jinja2 3.1.6, uvicorn 0.54.0.
- No SSH configuration and no POWER access. Only `GET /openapi.json`,
  `POST /analyze` and `POST /build-workflow` were called; none of them
  touches the cluster.

The exact requests are listed in `cases.json`. `MANIFEST.json` records the
SHA-256 of every file, raw and after normalisation.

## Regenerating and comparing (reviewer procedure)

1. Check out BMD Compute at `a746155`, install the pinned scientific runtime,
   and start the service. No SSH configuration is needed:

   ```
   uvicorn main:app --host 127.0.0.1 --port 8765
   ```

2. From the client repository:

   ```
   python tools/fixtures_v1.py verify      # committed files vs MANIFEST.json
   python tools/fixtures_v1.py capture --compute-url http://127.0.0.1:8765 --out /tmp/fresh
   python tools/fixtures_v1.py compare tests/fixtures/compute_v1.0.0 /tmp/fresh
   ```

`capture` uses only the client's own `Transport` and closed `Endpoint` table.
It imports no Compute code and can reach no other route.

`compare` reports each file as `identical`, `equal-after-N1..N4` or
`DIFFERENT`. The only normalisations, applied to both sides, are values
Compute generates freshly for every build request:

| Id | Value | Replaced by |
|---|---|---|
| N1 | Signed submission identity token in the build form | `<IDENTITY-TOKEN>` |
| N2 | UUIDs (submission attempt id, in forms and scripts) | `<UUID>` |
| N3 | Run timestamps `YYYYMMDD-HHMMSS` (run/job names, paths) | `<RUN-TIMESTAMP>` |
| N4 | 64-hex SHA-256 of the exact submission script (embeds N2/N3) | `<SHA256>` |

Result on 2026-10-03:

- Two fresh captures were compared against the committed files and against
  each other.
- Six files were byte-identical: `openapi.json`, the three analyze pages, and
  the two 400 build pages.
- The five successful build pages were equal after N1–N4.
- Nothing else differed.

A different Python, pymatgen or FastAPI version may change the HTML or the
generated inputs. That shows up as `DIFFERENT` and should be investigated, not
normalised away.

## Submission identity material in fixtures

The build pages contain the hidden submission identity token and attempt id
that Compute v1 embeds in its Prepare/Submit forms. They were signed with that
local process's ephemeral key and are worthless outside it. They are kept on
purpose: the tests use them to prove that the client never relays such
material.

`Si.POSCAR` is byte-identical to the client's identity probe structure.
