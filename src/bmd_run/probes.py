"""Fixed probe input and reference values for the v1.0.0 behavioural fingerprint.

The probe structure is a plain test input (two-atom diamond Si). It is not a
scientific choice: the client sends it to Compute and records what Compute
answers.

The reference hashes were recorded from a clean local checkout of BMD Compute
v1.0.0 (commit below), run with the pinned ``constraints/scientific-runtime.txt``.
A mismatch means that the service *behaves* differently from that recording.
It does not by itself identify which source the service runs.
"""

from __future__ import annotations

REFERENCE_RELEASE = "v1.0.0"
REFERENCE_COMMIT = "a746155b487f903a167eca6b3c92e860aaf8c7f5"

PROBE_STRUCTURE_FORMAT = "poscar"
PROBE_STRUCTURE = """Si
1.0
0.0 2.715 2.715
2.715 0.0 2.715
2.715 2.715 0.0
Si
2
direct
0.00 0.00 0.00
0.25 0.25 0.25
"""
PROBE_DESIRED_OUTPUT = "energy_only"

FINGERPRINT_ALGORITHM = "sha256 over canonical JSON (sorted keys, compact separators)"

REFERENCE_FINGERPRINTS = {
    "openapi_surface": "bc708f335e579ac13ee5b15244e530c46afb8cca0502e3b18404990c953d12bc",
    "option_catalogue": "2179f3cebc227913d9ef73f58d000ac9d9100217841ef54236df7ba3d9e17420",
    "probe_energy_plan": "07ffedbbe4d5a0ac9653d199e29d391884e30a8ff7440cb3ac394ecc0f08f759",
}
