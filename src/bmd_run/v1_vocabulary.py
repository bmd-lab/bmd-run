"""Closed vocabularies for the frozen BMD Compute v1.0.0 interface, owned by the client.

Every string the client reports is either a client constant or a member of one
of these finite sets. A value read from a Compute page is accepted only if it
equals a member exactly. The client then outputs its own copy of that member,
never the string it received.

These sets name the identifiers BMD Compute v1.0.0 publishes (sources noted
for commit a746155). They are used only to recognise values. The client makes
no scientific decision with them: which stages, theories, modifiers or
resources apply is decided by Compute.
"""

from __future__ import annotations

from types import MappingProxyType

# backend/calculations/registry.py desired_output_workflow_spec / calculation_form_options
DESIRED_OUTPUTS = ("energy_only", "relaxed_structure", "electronic_dos", "electronic_band_structure", "custom")
# backend/calculations/models.py StageType, Theory, Modifier
STAGE_TYPES = ("relax", "static", "dos", "band_structure")
THEORIES = ("pbe", "r2scan", "hse06")
MODIFIERS = ("soc", "dft_u", "dispersion", "spin_polarized", "gamma_only", "ions_only")
# VASP executables named in v1 previews and submission summaries
VASP_EXECUTABLES = ("vasp_std", "vasp_ncl", "vasp_gam")
# backend/config.py DEFAULT_PARTITION / DEFAULT_ACCOUNT; resources.py ALLOWED_QUEUES
PARTITIONS = ("leeburton-pool",)
ACCOUNTS = ("power-leeburton-users_v2",)
# backend/calculations/method_considerations.py, default_treatments.py, dft_u_policy.py
CONSIDERATION_IDS = (
    "spin.composition_screen",
    "dftu.mp_oxide_fluoride",
    "soc.heavy_elements",
    "dispersion.two_dimensional_connectivity",
)
CRYSTAL_SYSTEMS = ("triclinic", "monoclinic", "orthorhombic", "tetragonal", "trigonal", "hexagonal", "cubic")
KPOINT_MESH_MODES = MappingProxyType({"gamma": "Gamma", "monkhorst-pack": "Monkhorst-Pack", "monkhorst": "Monkhorst-Pack"})
KPOINT_COORDINATE_MODES = MappingProxyType({"reciprocal": "reciprocal", "cartesian": "cartesian"})

# Client-owned labels (presentation only).
DESIRED_OUTPUT_LABELS = MappingProxyType({
    "energy_only": "Energy",
    "relaxed_structure": "Relaxed structure",
    "electronic_dos": "Electronic density of states",
    "electronic_band_structure": "Electronic band structure",
    "custom": "Custom workflow",
})
STAGE_TYPE_LABELS = MappingProxyType({
    "relax": "Geometry Optimisation",
    "static": "Static Energy",
    "dos": "Density of States",
    "band_structure": "Band Structure",
})
THEORY_LABELS = MappingProxyType({"pbe": "PBE", "r2scan": "r2SCAN", "hse06": "HSE06"})
MODIFIER_LABELS = MappingProxyType({
    "soc": "Spin-Orbit Coupling",
    "dft_u": "DFT+U",
    "dispersion": "Dispersion correction",
    "spin_polarized": "Spin Polarised",
    "gamma_only": "Gamma-only",
    "ions_only": "Ions only",
})
CONSIDERATION_LABELS = MappingProxyType({
    "spin.composition_screen": "Spin polarisation",
    "dftu.mp_oxide_fluoride": "DFT+U",
    "soc.heavy_elements": "Spin-orbit coupling",
    "dispersion.two_dimensional_connectivity": "Dispersion correction",
})

# Exact v1 consideration titles (main.py _method_consideration_browser_name) -> client status.
# Any other title yields status "unrecognized"; the title text itself is never reported.
CONSIDERATION_TITLE_STATUS = MappingProxyType({
    ("spin.composition_screen", "Spin Polarisation applied"): "applied",
    ("dftu.mp_oxide_fluoride", "DFT+U applied"): "applied",
    ("soc.heavy_elements", "Spin-Orbit Coupling (SOC) applied"): "applied",
    ("dispersion.two_dimensional_connectivity", "van der Waals correction applied"): "applied",
    ("soc.heavy_elements", "Spin-Orbit Coupling (SOC) not used in geometry optimisation"): "not_applied_automatically",
    ("dftu.mp_oxide_fluoride", "DFT+U not applied automatically"): "not_applied_automatically",
    ("dispersion.two_dimensional_connectivity", "van der Waals Correction"): "advisory",
    ("spin.composition_screen", "Spin Polarisation"): "advisory",
    ("dftu.mp_oxide_fluoride", "DFT+U"): "advisory",
    ("soc.heavy_elements", "Spin-Orbit Coupling (SOC)"): "advisory",
})
CONSIDERATION_STATUSES = ("applied", "not_applied_automatically", "advisory", "unrecognized")

# Exact v1 failure-panel titles (templates/index.html) -> client error stage.
FAILURE_TITLE_STAGE = MappingProxyType({
    "Structure Validation Failed": "structure_validation",
    "Calculation Validation Failed": "calculation_validation",
})
ERROR_STAGES = ("structure_validation", "calculation_validation", "unrecognized")

ELEMENTS = (
    "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne", "Na", "Mg", "Al", "Si", "P", "S", "Cl", "Ar",
    "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn", "Ga", "Ge", "As", "Se", "Br",
    "Kr", "Rb", "Sr", "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd", "In", "Sn", "Sb", "Te",
    "I", "Xe", "Cs", "Ba", "La", "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm",
    "Yb", "Lu", "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg", "Tl", "Pb", "Bi", "Po", "At", "Rn",
    "Fr", "Ra", "Ac", "Th", "Pa", "U", "Np", "Pu", "Am", "Cm", "Bk", "Cf", "Es", "Fm", "Md", "No", "Lr",
    "Rf", "Db", "Sg", "Bh", "Hs", "Mt", "Ds", "Rg", "Cn", "Nh", "Fl", "Mc", "Lv", "Ts", "Og",
)


def stage_label(stage_type: str, theory: str, modifiers) -> str:
    """Client-reconstructed stage name, e.g. 'HSE06 Static Energy + Spin Polarised'."""

    label = f"{THEORY_LABELS[theory]} {STAGE_TYPE_LABELS[stage_type]}"
    if modifiers:
        label += " + " + ", ".join(MODIFIER_LABELS[m] for m in modifiers)
    return label
