# BMD Compute machine API v1: Automatic Resource Selection R1 fixtures

Recorded from bmd-compute `90d1c2391760af03730769efd1e23285d46f1b30` ("Add automatic
hardware resource selection R1", on bmd-compute main), which introduces plan digest
version 2, `resources.qos`, `resource_selection` and walltimes with up to four hour
digits. They are kept apart from `../compute_api_v1/`, which stays pinned to its own
reference commit.

Recorded in-process with Compute's fake POWER (no network, no token material) by

    python tools/record_machine_api_fixtures.py record-automatic-resources --compute-checkout PATH

run in that Compute checkout's environment; `compare-automatic-resources` re-checks them.
(An earlier recording from the same change before it was committed produced byte-identical
responses.)

* `plan_si_energy_automatic`: no resources sent (all Automatic);
* `plan_si_energy_partial`: `cpus` given, the rest Automatic;
* `plan_si_energy_manual_2400h`: `cpus`, `memory_gb` and `walltime: "2400:00:00"` given;
* `attempt_prepare_automatic`, `attempt_submit_automatic`: the automatic plan prepared
  and submitted (attempt resources include QOS).

Normalisations M1 and M2 of `../compute_api_v1/RECORDING.md` apply.
