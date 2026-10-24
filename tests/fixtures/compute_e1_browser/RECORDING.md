# Recorded BMD Compute E1 browser responses (Relaxed Energy)

Verbatim HTTP response bodies from bmd-compute
`228cc59502ebc118c3d4128cbce8652e04b88f72` (main after PR #53, E1), the first
Compute whose browser defaults to the `relaxed_energy` Desired Output and no
longer offers `energy_only` in its menu (`energy_only` remains a valid
identifier: a request that names it still builds the single-point static).

- Started with `uvicorn main:app --host 127.0.0.1 --port 8765`, scientific
  runtime from that checkout's `constraints/scientific-runtime.txt`, Python
  3.12, FastAPI 0.142.2, Starlette 1.7.0, Jinja2 3.1.6, uvicorn 0.54.0.
- No SSH configuration and no POWER access. Only `POST /analyze` and
  `POST /build-workflow` were called (`cases.json`).

Captured and checked with the same tool and normalisations (N1-N4) as the
v1.0.0 set:

    python tools/fixtures_v1.py --fixtures tests/fixtures/compute_e1_browser capture --compute-url http://127.0.0.1:8765 --out /tmp/fresh
    python tools/fixtures_v1.py --fixtures tests/fixtures/compute_e1_browser compare tests/fixtures/compute_e1_browser /tmp/fresh
    python tools/fixtures_v1.py --fixtures tests/fixtures/compute_e1_browser verify

Two captures were compared: the analyze page and the 400 build page were
byte-identical, the three successful build pages equal after N1-N4.

The build pages contain the per-process submission identity token and attempt
id Compute embeds in its forms (worthless outside that process); the tests use
them to prove the client never relays such material.
