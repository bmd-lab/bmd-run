# Contributing

Keep contributions small and preserve `bmd-run`'s authority boundary:

- `bmd-compute` remains the methodology and execution authority.
- `bmd-run` may call only `GET /openapi.json`, `POST /analyze` and
  `POST /build-workflow` (frozen v1), and, with an API token,
  `POST /api/v1/plans` and `PUT`/`GET /api/v1/attempts/{uuid}`.
- Do not add Monitor, Resume, cancellation, result retrieval, SSH, SFTP, SLURM,
  tunnel, subprocess, Git, database or arbitrary HTTP capability.
- Resumed requests must come from the persisted attempt record, never from CLI
  arguments; never generate a new attempt after a timeout or uncertain submission.
- Do not duplicate scientific methodology that belongs in `bmd-compute`.
- Do not scrub legitimate Compute-resolved provenance merely because it is
  site-specific.

Before proposing a change, run:

```bash
python -m unittest discover -s tests
python tools/fixtures_v1.py verify
git diff --check
```

Machine-API fixtures are re-recorded and compared from a bmd-compute checkout's
own environment (see `tests/fixtures/compute_api_v1/RECORDING.md`):

```bash
python tools/record_machine_api_fixtures.py compare --compute-checkout PATH
```

For security-sensitive changes, include an adversarial test that would fail if
the boundary regressed.
