"""The transitional v1 adapter against pages recorded from BMD Compute v1.0.0."""

from __future__ import annotations

import unittest

from _support import fixture_text

from bmd_run import v1_vocabulary as vocab
from bmd_run.errors import UnexpectedResponse
from bmd_run.v1_html import Page
from bmd_run.v1_inputs import parse_incar, parse_kpoints, render_incar


def page(name):
    return Page(fixture_text(name))


class Extraction(unittest.TestCase):
    def test_analyze_si(self):
        p = page("analyze_si.html")
        structure = p.structure()
        self.assertEqual(structure["formula"], "Si2")
        self.assertEqual(structure["reduced_formula"], "Si")
        self.assertEqual(structure["composition"], [{"element": "Si", "count": 2}])
        self.assertEqual(structure["natoms"], 2)
        self.assertEqual(structure["volume_angstrom3"], 40.026)
        self.assertEqual(structure["space_group_number"], 227)
        self.assertEqual(structure["crystal_system"], "cubic")
        self.assertEqual(
            p.workflow(),
            {"desired_output": "energy_only",
             "stages": [{"index": 1, "stage_type": "static", "theory": "pbe", "modifiers": [],
                         "label": "PBE Static Energy"}]},
        )
        catalogue = p.option_catalogue()
        self.assertEqual([d["value"] for d in catalogue["desired_outputs"]], list(vocab.DESIRED_OUTPUTS))
        self.assertEqual(catalogue["unrecognized_entry_count"], 0)
        self.assertIsNone(p.failure_stage())

    def test_dos_resolves_to_pbe_relax_hse06_static_hse06_dos(self):
        p = page("build_si_electronic_dos.html")
        self.assertEqual(
            [s["label"] for s in p.workflow()["stages"]],
            ["PBE Geometry Optimisation", "HSE06 Static Energy", "HSE06 Density of States"],
        )
        self.assertEqual(p.stage_executables(3), {1: "vasp_std", 2: "vasp_std", 3: "vasp_std"})

    def test_nio_automatic_treatments_are_bounded(self):
        p = page("build_nio_electronic_dos.html")
        self.assertEqual(
            p.considerations(),
            [{"id": "spin.composition_screen", "label": "Spin polarisation", "status": "applied"},
             {"id": "dftu.mp_oxide_fluoride", "label": "DFT+U", "status": "applied"}],
        )
        stage = p.workflow()["stages"][0]
        self.assertEqual(stage["modifiers"], ["dft_u", "spin_polarized"])
        self.assertEqual(stage["label"], "PBE Geometry Optimisation + DFT+U, Spin Polarised")

    def test_generated_inputs_all_stages(self):
        p = page("build_nio_electronic_dos.html")
        stages = p.input_stages(3)
        self.assertEqual([s["index"] for s in stages], [1, 2, 3])
        tags = {t["tag"]: t["value"] for t in stages[0]["incar"]["tags"]}
        self.assertEqual(tags["ENCUT"], 580.0)
        self.assertEqual(tags["LDAUU"], [{"repeat": None, "value": 6.2}, {"repeat": None, "value": 0}])
        self.assertEqual(tags["MAGMOM"], [{"repeat": 1, "value": 5.0}, {"repeat": 1, "value": 0.6}])
        self.assertEqual(stages[2]["kpoints"],
                         {"kind": "automatic_mesh", "mode": "Gamma", "subdivisions": [11, 11, 11], "shift": None})
        band = page("build_si_electronic_band_structure.html").input_stages(3)
        self.assertEqual(band[2]["kpoints"], {"kind": "explicit_list", "count": 334, "coordinate_mode": "reciprocal"})
        single = page("build_si_energy_only.html").input_stages(1)
        self.assertEqual(len(single), 1)
        self.assertIn("ENCUT = 620.0", single[0]["incar"]["text"])

    def test_resources(self):
        self.assertEqual(
            page("build_si_energy_only.html").resources(),
            {"partition": "leeburton-pool", "account": "power-leeburton-users_v2", "cpus": 24,
             "nodes": 1, "memory_gb": 128, "walltime": "72:00:00"},
        )

    def test_failure_stage_only(self):
        self.assertEqual(page("analyze_bad.html").failure_stage(), "structure_validation")
        self.assertEqual(page("build_si_badcpus.html").failure_stage(), "calculation_validation")
        self.assertEqual(page("build_si_unknown.html").failure_stage(), "calculation_validation")

    def test_missing_sections_are_unexpected(self):
        empty = Page("<html><body></body></html>")
        for accessor in (empty.workflow, empty.option_catalogue, empty.structure, empty.resources,
                         lambda: empty.input_stages(1)):
            with self.subTest(accessor=accessor), self.assertRaises(UnexpectedResponse):
                accessor()


class BoundedInputParsers(unittest.TestCase):
    def test_incar_rebuilt_from_tokens(self):
        parsed = parse_incar(["ENCUT = 520", "ALGO = fast", "LREAL = Auto", "MAGMOM = 2*0.6 1.5",
                              "SYSTEM = anything at all", "NOTATAG = 3"], "t")
        self.assertEqual(parsed["withheld_tag_count"], 2)
        self.assertEqual(parsed["text"], "ENCUT = 520.0\nALGO = Fast\nLREAL = Auto\nMAGMOM = 2*0.6 1.5")

    def test_incar_rejects_non_assignments_and_bad_values(self):
        for lines in (["# comment"], ["ENCUT = 520 eV"], ["ENCUT = abc"], ["ISPIN = 2.5"], ["LWAVE = maybe"],
                      ["ENCUT = 1.234567890123456"], ["ENCUT = 1", "ENCUT = 2"], ["encut = 520"]):
            with self.subTest(lines=lines), self.assertRaises(UnexpectedResponse):
                parse_incar(lines, "t")

    def test_unknown_words_are_withheld_not_reported(self):
        parsed = parse_incar(["ALGO = eyJhdHRlbXB0", "PREC = Accurate"], "t")
        self.assertEqual(parsed["withheld_tag_count"], 1)
        self.assertEqual(render_incar(parsed["tags"]), "PREC = Accurate")

    def test_kpoints(self):
        self.assertEqual(parse_kpoints(["anything", "0", "Monkhorst-Pack", "4 4 4"], "k")["mode"], "Monkhorst-Pack")
        for lines in (["c", "0", "Gamma", "4 4"], ["c", "0", "Gammaa", "4 4 4"], ["c", "2", "Reciprocal", "0 0 0 1"],
                      ["c", "0", "Gamma", "4 4 4", "0 0 0", "extra"]):
            with self.subTest(lines=lines), self.assertRaises(UnexpectedResponse):
                parse_kpoints(lines, "k")


if __name__ == "__main__":
    unittest.main()
