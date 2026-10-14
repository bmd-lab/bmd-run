# BMD Compute machine API v1: phonon plan fixture

`plan_si_poscar_phonons.json` is `POST /api/v1/plans` for `Si.POSCAR` with
`desired_output: "phonons"`, recorded from bmd-compute
`93cfe16b212d356e271e62c4805433d3a9f19046` (main after PR #46, which added the
phonon Desired Output). It is kept apart from `../compute_api_v1/`, which stays
pinned to its own reference commit.

Recorded the same way as the main set (in-process, fake POWER, no network, no
token material), with:

    python tools/record_machine_api_fixtures.py record-phonons --compute-checkout PATH

run from that Compute checkout's environment (`compare-phonons` re-checks it).
`MANIFEST.json` lists the file's SHA-256.
