# Contributing

Keep contributions small and preserve `bmd-run`'s authority boundary:

- `bmd-compute` remains the methodology and execution authority.
- `bmd-run` may call only `GET /openapi.json`, `POST /analyze` and
  `POST /build-workflow`.
- Do not add Prepare, Submit, Monitor, Resume, SSH, SFTP, SLURM, subprocess,
  Git, database or arbitrary HTTP capability.
- Do not duplicate scientific methodology that belongs in `bmd-compute`.
- Do not scrub legitimate Compute-resolved provenance merely because it is
  site-specific.

Before proposing a change, run:

```bash
python -m unittest discover -s tests
python tools/fixtures_v1.py verify
git diff --check
```

For security-sensitive changes, include an adversarial test that would fail if
the boundary regressed.
