"""Closed vocabularies for BMD Compute's machine API v1, owned by the client.

Pinned to bmd-compute ``e3fbb3b`` (``docs/machine_api.md``, ``compute_api/``,
``backend/calculations/``). As with the frozen v1 adapter (``v1_vocabulary``),
a string received from Compute is reported only if it equals a member of one of
these sets, and the client then reports its own copy of that member. Anything
else is counted as unrecognised and not shown.

These sets are used to recognise values. The client makes no scientific
decision with them: Compute alone resolves workflows, treatments, inputs and
resources.
"""

from __future__ import annotations

from types import MappingProxyType

from . import v1_vocabulary as v1

API_VERSION = "v1"
PLAN_SCHEMA = "bmd_compute.api.plan"
ATTEMPT_SCHEMA = "bmd_compute.api.attempt"
RESPONSE_SCHEMA_VERSION = 1
PLAN_DIGEST_VERSIONS = (1,)

# The client's request vocabulary (compute_api/schemas.py).
STRUCTURE_FORMATS = ("poscar", "cif")
WORKFLOW_MODES = ("desired_output", "custom")

# backend/calculations/models.py at e3fbb3b (phonon_forces is representation only).
STAGE_TYPES = v1.STAGE_TYPES + ("phonon_forces",)
THEORIES = v1.THEORIES
MODIFIERS = v1.MODIFIERS
VASP_EXECUTABLES = v1.VASP_EXECUTABLES
CRYSTAL_SYSTEMS = v1.CRYSTAL_SYSTEMS
ELEMENTS = v1.ELEMENTS
PARTITIONS = v1.PARTITIONS
ACCOUNTS = v1.ACCOUNTS
DESIRED_OUTPUTS = ("energy_only", "relaxed_structure", "electronic_dos", "electronic_band_structure")

# backend/calculations/method_considerations.py, default_treatments.py, dft_u_policy.py
CONSIDERATION_IDS = v1.CONSIDERATION_IDS
CONSIDERATION_STATUSES = ("recommended_for_consideration", "suppressed_by_d0_gate")
SELECTION_STATES = ("not_selected", "already_selected")
AUTOMATIC_APPLICATION_STATES = ("applied", "omitted_unsupported_combination", "not_applicable", "advisory")
CONSIDERATION_METHODS = ("dft_u", "soc", "spin_polarisation", "dispersion")

# pymatgen Kpoints.supported_modes names, and POTCAR functionals / symbol suffixes.
KPOINT_STYLES = ("Gamma", "Monkhorst", "Automatic", "Line_mode", "Cartesian", "Reciprocal")
POTCAR_FUNCTIONALS = ("PBE", "PBE_52", "PBE_54", "PBE_64", "LDA", "LDA_52", "LDA_54", "LDA_64", "PW91")
POTCAR_SUFFIXES = (
    "pv", "sv", "d", "h", "s", "f", "3", "2", "GW", "sv_GW", "pv_GW", "d_GW", "h_GW", "s_GW",
    "AE", "nc_GW", "d_sv_GW", "f_GW", "sv_h", "pv_h", "d_h",
)

# backend/runtime_environment.py PARITY_CRITICAL_PACKAGES
PARITY_PACKAGES = ("atomate2", "pymatgen", "pymatgen-core", "custodian", "emmet-core", "jobflow", "spglib")
# compute_api/plan_digest.py policy_versions()
POLICY_NAMES = (
    "new_calculation_admission",
    "automatic_dft_u",
    "method_considerations",
    "custodian",
    "runtime_parity",
    "plan_digest",
)

# compute_api/execution.py
ATTEMPT_STATES = ("registered", "prepared", "submitted", "submission_uncertain")
SCHEDULER_SUMMARIES = ("PENDING", "RUNNING", "SUCCESS", "FAILURE", "UNKNOWN")
SLURM_STATES = (
    "PENDING", "CONFIGURING", "RUNNING", "COMPLETING", "COMPLETED", "FAILED",
    "TIMEOUT", "CANCELLED", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED",
    "BOOT_FAIL", "DEADLINE", "SUSPENDED", "REQUEUED", "REQUEUE_HOLD",
    "REQUEUE_FED", "RESIZING", "SIGNALING", "STAGE_OUT", "SPECIAL_EXIT",
    "STOPPED", "REVOKED", "RESV_DEL_HOLD", "UNKNOWN",
)

# docs/machine_api.md and compute_api/router.py / execution.py: error code -> HTTP statuses.
ERROR_STATUS = MappingProxyType({
    "query_not_allowed": (400,),
    "invalid_json": (400,),
    "unauthenticated": (401,),
    "loopback_required": (403,),
    "insufficient_scope": (403,),
    "attempt_forbidden": (403,),
    "attempt_not_found": (404,),
    "plan_digest_mismatch": (409,),
    "attempt_request_mismatch": (409,),
    "plan_changed": (409,),
    "attempt_fingerprint_mismatch": (409,),
    "runtime_package_changed": (409,),
    "attempt_in_progress": (409,),
    "attempt_busy": (409,),
    "submission_not_started": (409,),
    "submission_uncertain": (409,),
    "remote_state_invalid": (409,),
    "request_too_large": (413,),
    "unsupported_media_type": (415,),
    "invalid_request": (422,),
    "structure_invalid": (422,),
    "calculation_invalid": (422,),
    "invalid_attempt_id": (422,),
    "resource_limit_exceeded": (422,),
    "active_job_cap_exceeded": (429,),
    "submission_cap_exceeded": (429,),
    "attempt_cap_exceeded": (429,),
    "plan_failed": (500,),
    "attempt_failed": (500,),
    "lookup_failed": (500,),
    "server_misconfigured": (500,),
    "prepare_failed": (502,),
    "submit_failed": (502,),
    "api_not_configured": (503,),
    "execution_not_configured": (503,),
    "remote_busy": (503,),
    "remote_unavailable": (503,),
    "remote_state_unconfirmed": (503,),
    "submission_outcome_unconfirmed": (503,),
})

# Compute diagnostic codes on calculation_invalid errors.
DIAGNOSTIC_CODES = (
    "unknown_desired_output",
    "hse06_soc_not_supported_for_new_calculations",
    "automatic_dispersion_dimensionality_analysis_failed",
    "unsupported_combination",
)

# compute_api/schemas.py field problem codes (invalid_request ``fields``).
FIELD_PROBLEMS = (
    "unknown_field",
    "required",
    "must_be_object",
    "must_be_poscar_or_cif",
    "must_be_nonempty_string",
    "too_large",
    "invalid_characters",
    "exactly_one_of_desired_output_or_custom",
    "must_be_identifier",
    "must_be_positive_integer",
    "must_be_hh_mm_ss",
    "must_be_nonempty_list",
    "too_many_stages",
    "must_be_identifier_list",
    "must_be_sha256_digest",
    "must_be_boolean",
    "must_be_label",
)
