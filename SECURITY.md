# Security Policy

`bmd-run` is intentionally restricted. Its read/build commands use three frozen
`bmd-compute` v1.0.0 routes. Its authenticated `bmd-run api` commands use three
`bmd-compute` machine-API routes to plan, prepare, submit and read one
calculation; `bmd-compute` performs all execution. `bmd-run` must not gain SSH,
SFTP, SLURM, tunnel, subprocess, Git, database or arbitrary HTTP authority, and
must not monitor, resume, cancel or retrieve results.

Please report suspected security issues privately before opening a public issue.
Use GitHub private vulnerability reporting if it is enabled for the repository,
or contact the maintainers listed in `pyproject.toml`.

Useful reports include:

- a way for `bmd-run` to contact any host other than the configured `bmd-compute`
  origin;
- a way for `bmd-run` to call any Compute route other than `GET /openapi.json`,
  `POST /analyze`, `POST /build-workflow`, `POST /api/v1/plans`, or
  `PUT`/`GET /api/v1/attempts/{uuid}`;
- a way for the API token to reach output, logs, attempt records, URLs or any
  origin other than the loopback tunnel or a reviewed HTTPS deployment;
- a way to make `bmd-run` submit an attempt twice, create a new attempt after an
  uncertain submission, or resume a request that differs from the recorded one
  without any local edit to the record (deliberate editing of one's own local
  records is outside the threat model; see `ARCHITECTURE.md`);
- leakage of Compute submission identity tokens, attempt fingerprints, SSH
  profiles or remote paths;
- local command execution, arbitrary imports or filesystem writes (other than the
  private attempt records) caused by input files, Compute responses or CLI
  arguments;
- accidental credentials, private keys or operational secrets in repository
  content.

Security boundaries in `bmd-compute` itself should be reported against
`bmd-compute`, but reports that involve both projects are welcome.
