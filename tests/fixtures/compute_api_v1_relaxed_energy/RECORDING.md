# BMD Compute machine API v1: Relaxed Energy fixtures (E1)

Recorded from bmd-compute `228cc59502ebc118c3d4128cbce8652e04b88f72` (main after
PR #53, which added the `relaxed_energy` Desired Output: PBE Geometry
Optimisation -> PBE Geometry Optimisation -> PBE Static Energy). Kept apart from
the other sets, which stay pinned to their own commits.

| File | Request |
|---|---|
| `plan_si_poscar_relaxed_energy.json` | `Si.POSCAR`, `desired_output: relaxed_energy`, automatic resources |
| `plan_nio_poscar_relaxed_energy.json` | `NiO.POSCAR`, `relaxed_energy` (DFT+U and spin on every stage) |
| `plan_si_relaxed_energy_manual.json` | `Si.POSCAR`, `relaxed_energy`, 48 CPUs, 64 GB, 12:00:00 |
| `plan_si_poscar_energy_only_e1.json` | `Si.POSCAR`, the historical single point `energy_only`, on the same Compute |
| `plan_si_custom_relax_relax_static.json` | a Custom PBE relax -> relax -> static (stays Custom: no recipe) |
| `plan_error_unknown_relaxed_energies.json` | an unknown identifier (`relaxed_energies`) |
| `attempt_prepare_relaxed_energy.json`, `attempt_submit_relaxed_energy.json`, `attempt_get_relaxed_energy.json` | prepare, submit and read one Relaxed Energy attempt, exactly as bmd-run sends them |

Recorded the same way as the other sets (in-process, Compute's in-memory fake
POWER, no network, no token material), with:

    python tools/record_machine_api_fixtures.py record-relaxed-energy --compute-checkout PATH

run from that Compute checkout's environment (`compare-relaxed-energy` re-records
and compares; it reported `compare ok`). `MANIFEST.json` lists each file's SHA-256.
