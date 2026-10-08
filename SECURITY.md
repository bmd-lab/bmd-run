# Security Policy

`bmd-run` is intentionally restricted to read/build interactions with `bmd-compute`.
It must not prepare, submit, monitor or resume calculations, and it must not gain
SSH, SFTP, SLURM, subprocess, Git, database or arbitrary HTTP authority.

Please report suspected security issues privately before opening a public issue.
Use GitHub private vulnerability reporting if it is enabled for the repository,
or contact the maintainers listed in `pyproject.toml`.

Useful reports include:

- a way for `bmd-run` to contact any host other than the configured `bmd-compute`
  origin;
- a way for `bmd-run` to call any Compute route other than `GET /openapi.json`,
  `POST /analyze` or `POST /build-workflow`;
- leakage of Compute submission tokens, attempt IDs, monitor state or job IDs;
- local command execution, arbitrary imports or filesystem writes caused by
  input files, Compute HTML or CLI arguments;
- accidental credentials, private keys or operational secrets in repository
  content.

Security boundaries in `bmd-compute` itself should be reported against
`bmd-compute`, but reports that involve both projects are welcome.
