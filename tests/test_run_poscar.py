"""R2.1: bare ``bmd-run`` (./POSCAR autodetection) and the workflow shortcuts.

In-process against the fake BMD Compute (``_api_support``) over real HTTP on
127.0.0.1, in temporary directories. Nothing contacts a live VM or POWER.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil

from _api_support import fixture
from test_run_command import SI_DIGEST, RunBase

from bmd_run import __version__, cli, errors

SI_TEXT_FIXTURE = "plan_si_poscar_energy_only"


class PoscarBase(RunBase):
    """RunBase, with the process working directory set to an empty temporary ``work`` directory."""

    def setUp(self):
        super().setUp()
        self.work = self.ws.root / "work"
        self.work.mkdir()
        self._cwd = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, self._cwd)

    def poscar(self, source="Si.POSCAR", name="POSCAR"):
        path = self.work / name
        shutil.copy2(self.ws.files / source, path)
        return path

    def custom_file(self):
        path = self.ws.root / "workflow.json"
        path.write_text(json.dumps({"stages": [{"stage_type": "relax", "theory": "pbe"},
                                               {"stage_type": "static", "theory": "pbe"}]}), encoding="utf-8")
        return path

    def plan_body(self, index=0):
        return json.loads(self.compute.requests[index]["body"])

    def assert_nothing_sent(self):
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.ws.records(), {})
        self.assertEqual(self.compute.sbatch_calls, 0)


class BareRun(PoscarBase):
    def test_bare_run_uses_local_poscar_as_energy_only(self):
        path = self.poscar()
        before = (os.stat(path).st_mtime_ns, path.read_bytes(), sorted(os.listdir(self.work)))
        code, out, err = self.run_cli()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.plan_body(), fixture(SI_TEXT_FIXTURE)["request"]["body"])
        (attempt, record), = self.ws.records().items()
        self.assertEqual(self.calls(), [("POST", "/api/v1/plans"), ("PUT", f"/api/v1/attempts/{attempt}"),
                                        ("PUT", f"/api/v1/attempts/{attempt}")])
        self.assertEqual([b["submit"] for b in self.puts()], [False, True])
        self.assertEqual(record["expected_plan_digest"], SI_DIGEST)
        self.assertEqual(record["local_state"], "submitted")
        self.assertEqual(record["structure_source"]["file_name"], "POSCAR")
        self.assertEqual(self.compute.sbatch_calls, 1)
        self.assertIn("Workflow:     Energy only", out)
        self.assertIn(f"bmd-run api status {attempt}", out)
        # The file is neither modified, renamed nor copied; nothing is written next to it.
        self.assertEqual((os.stat(path).st_mtime_ns, path.read_bytes(), sorted(os.listdir(self.work))), before)

    def test_bare_run_json_matches_the_r2_contract(self):
        self.poscar()
        code, out, _ = self.run_cli("--json")
        self.assertEqual(code, 0)
        document = json.loads(out)
        self.assertEqual((document["command"], document["stage"], document["ok"]), ("run", "complete", True))
        self.assertEqual(document["request"]["desired_output"], "energy_only")
        self.assertEqual(document["request"]["structure_source"]["file_name"], "POSCAR")
        self.assertEqual(document["recovery"], [f"bmd-run api status {document['attempt_id']}"])
        self.assertNotIn(str(self.work), out)

    def test_without_poscar_it_fails_before_anything_is_sent(self):
        for text_mode in (True, False):
            code, out, err = self.run_cli(*([] if text_mode else ["--json"]))
            self.assertEqual(code, errors.EXIT_USAGE)
            message = err if text_mode else json.loads(out)["error"]["message"]
            self.assertIn("no POSCAR file in the current directory", message)
        self.assert_nothing_sent()

    def test_never_searches_parents_subdirectories_or_similar_names(self):
        shutil.copy2(self.ws.files / "Si.POSCAR", self.ws.root / "POSCAR")  # parent directory
        sub = self.work / "sub"
        sub.mkdir()
        shutil.copy2(self.ws.files / "Si.POSCAR", sub / "POSCAR")
        for name in ("poscar", "POSCAR.vasp", "POSCAR.bak", "CONTCAR", "Si.cif"):
            shutil.copy2(self.ws.files / "Si.POSCAR", self.work / name)
        code, _, err = self.run_cli()
        self.assertEqual(code, errors.EXIT_USAGE, err)
        self.assertIn("no POSCAR file in the current directory", err)
        self.assert_nothing_sent()

    def test_a_directory_named_poscar_is_not_a_structure(self):
        (self.work / "POSCAR").mkdir()
        code, _, err = self.run_cli()
        self.assertEqual(code, errors.EXIT_USAGE)
        self.assertIn("no POSCAR file in the current directory", err)
        self.assert_nothing_sent()

    def test_invalid_poscar_creates_no_attempt(self):
        (self.work / "POSCAR").write_text("not a structure\n", encoding="utf-8")
        code, out, _ = self.run_cli("--json")
        self.assertEqual(code, errors.EXIT_INVALID_REQUEST)
        document = json.loads(out)
        self.assertEqual((document["stage"], document["attempt_id"], document["recovery"]), ("plan", None, []))
        self.assertEqual(self.ws.records(), {})
        self.assertNotIn("PUT", [m for m, _ in self.calls()])

    def test_empty_poscar_creates_no_attempt(self):
        (self.work / "POSCAR").write_bytes(b"")
        code, _, _ = self.run_cli()
        self.assertNotEqual(code, 0)
        self.assertEqual(self.ws.records(), {})
        self.assertNotIn("PUT", [m for m, _ in self.calls()])

    def test_format_needs_an_explicit_file(self):
        self.poscar()
        for argv in (["--format", "cif"], ["--format=poscar"]):
            code, _, err = self.run_cli(*argv)
            self.assertEqual(code, errors.EXIT_USAGE, argv)
            self.assertIn("--format applies only to an explicit structure file", err)
        self.assert_nothing_sent()

    def test_repeated_bare_runs_are_separate_calculations(self):
        self.poscar()
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(len(self.ws.records()), 2)
        self.assertEqual(self.compute.sbatch_calls, 2)
        self.assertEqual(len([b for b in self.puts() if b["submit"]]), 2)

    def test_resource_overrides_apply_to_the_local_poscar(self):
        self.poscar()
        code, _, err = self.run_cli("--cpus", "48", "--memory-gb", "64", "--walltime", "12:00:00")
        self.assertEqual(code, 0, err)
        sent = self.plan_body()
        self.assertEqual(sent, fixture("plan_si_energy_only_resources")["request"]["body"])
        self.assertEqual({k: v for k, v in self.puts()[-1].items() if k not in ("submit", "expected_plan_digest")}, sent)
        before = len(self.compute.requests)
        self.run_cli("--queue", "short")
        self.assertEqual(self.plan_body(before)["resources"], {"queue": "short"})

    def test_api_url_state_dir_and_token_file_overrides(self):
        self.poscar()
        state = self.ws.root / "other-state"
        environ = {"HOME": str(self.ws.root), "BMD_RUN_API_URL": "http://127.0.0.1:9"}
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["--api-url", self.server.url, "--state-dir", str(state), "--token-file",
                         str(self.token_file), "--timeout", "5"], environ=environ, stdout=out, stderr=err)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(len(list((state / "attempts").glob("*.json"))), 1)
        self.assertEqual(self.ws.records(), {})
        self.assertNotIn(self.token, out.getvalue() + err.getvalue())

    def test_lost_prepare_response_keeps_r2_recovery(self):
        self.poscar()
        self.compute.add_fault(None)
        self.compute.add_fault("drop_after_processing")
        code, _, err = self.run_cli()
        self.assertEqual(code, errors.EXIT_SERVICE_UNAVAILABLE)
        (attempt, record), = self.ws.records().items()
        self.assertEqual(record["local_state"], "prepare_unconfirmed")
        self.assertEqual([b["submit"] for b in self.puts()], [False])
        self.assertIn(f"bmd-run api prepare --attempt {attempt}", err)
        for argv in (["api", "prepare", "--attempt", attempt], ["api", "submit", attempt]):
            environ = {"HOME": str(self.ws.root), "BMD_RUN_STATE_DIR": str(self.ws.state),
                       "BMD_RUN_API_URL": self.server.url}
            self.assertEqual(cli.main(argv + ["--token-file", str(self.token_file)], environ=environ,
                                      stdout=io.StringIO(), stderr=io.StringIO()), 0)
        self.assertEqual(self.compute.sbatch_calls, 1)
        self.assertEqual(list(self.ws.records()), [attempt])

    def test_uncertain_submission_is_not_retried(self):
        self.poscar()
        self.compute.add_fault(None)
        self.compute.add_fault(None)
        self.compute.add_fault("timeout_after_processing")
        code, out, _ = self.run_cli("--json", timeout="0.5")
        self.assertEqual(code, errors.EXIT_SUBMISSION_UNCERTAIN)
        document = json.loads(out)
        self.assertEqual(document["recovery"], [f"bmd-run api status {document['attempt_id']}",
                                                f"bmd-run api submit {document['attempt_id']}"])
        self.assertEqual([b["submit"] for b in self.puts()], [False, True])
        self.assertNotIn("Retry the same command", out)

    def test_attempt_is_recorded_before_preparation(self):
        self.poscar()
        seen = []
        self.compute.observers.append(
            lambda method, path, body: seen.append(dict(self.ws.records())) if method == "PUT" else None)
        self.run_cli()
        (record,) = seen[0].values()
        self.assertEqual((record["local_state"], record["expected_plan_digest"]), ("planned", SI_DIGEST))


class Shortcuts(PoscarBase):
    def test_each_shortcut_sends_its_compute_identifier(self):
        self.poscar()
        for flag, identifier in (("--relax", "relaxed_structure"), ("--dos", "electronic_dos"),
                                 ("--bands", "electronic_band_structure")):
            before = len(self.compute.requests)
            self.run_cli(flag)
            self.assertEqual(self.plan_body(before)["workflow"], {"desired_output": identifier}, flag)
            self.assertEqual(self.plan_body(before)["structure"], fixture(SI_TEXT_FIXTURE)["request"]["body"]["structure"])
        self.assertEqual(cli.DESIRED_OUTPUT_SHORTCUTS, {"--relax": "relaxed_structure", "--dos": "electronic_dos",
                                                        "--bands": "electronic_band_structure"})

    def test_dos_shortcut_runs_a_recorded_calculation_end_to_end(self):
        self.poscar("NiO.POSCAR")
        code, out, err = self.run_cli("--dos", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.plan_body(), fixture("plan_nio_poscar_electronic_dos")["request"]["body"])
        self.assertEqual(json.loads(out)["request"]["desired_output"], "electronic_dos")
        self.assertEqual(self.compute.sbatch_calls, 1)

    def test_shortcuts_with_an_explicit_file_and_in_any_position(self):
        nio = str(self.ws.files / "NiO.POSCAR")
        for argv in ([nio, "--dos"], ["--dos", nio], ["--json", "--dos", nio]):
            before = len(self.compute.requests)
            code, _, err = self.run_cli(*argv)
            self.assertEqual(code, 0, (argv, err))
            self.assertEqual(self.plan_body(before), fixture("plan_nio_poscar_electronic_dos")["request"]["body"])

    def test_shortcuts_equal_the_explicit_desired_output(self):
        self.poscar()
        bodies = []
        for argv in (["--relax"], ["--desired-output", "relax"], ["--desired-output", "relaxed_structure"]):
            before = len(self.compute.requests)
            self.run_cli(*argv)
            bodies.append(self.plan_body(before))
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(bodies[0], bodies[2])

    def test_unrecorded_plan_for_a_shortcut_creates_no_attempt(self):
        # The fake has no recorded relax/bands plan, so it refuses them: nothing is prepared.
        self.poscar()
        for flag in ("--relax", "--bands"):
            code, _, _ = self.run_cli(flag)
            self.assertEqual(code, errors.EXIT_INVALID_REQUEST, flag)
        self.assertEqual(self.ws.records(), {})
        self.assertNotIn("PUT", [m for m, _ in self.calls()])

    def test_custom_shortcut_is_the_custom_workflow(self):
        self.poscar()
        custom = str(self.custom_file())
        for argv in (["--custom", custom], [f"--custom={custom}"], ["--custom-workflow", custom]):
            before = len(self.compute.requests)
            code, out, err = self.run_cli(*argv)
            self.assertEqual(code, 0, (argv, err))
            self.assertEqual(self.plan_body(before), fixture("plan_si_custom_relax_static")["request"]["body"])
            self.assertIn("Workflow:     Custom workflow", out)
        self.assertEqual(self.compute.sbatch_calls, 3)

    def test_custom_file_named_like_a_command_is_a_value(self):
        self.poscar()
        shutil.copy2(self.custom_file(), self.work / "status")
        code, out, err = self.run_cli("--custom", "status")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.plan_body(), fixture("plan_si_custom_relax_static")["request"]["body"])

    def test_missing_custom_file_sends_nothing(self):
        self.poscar()
        code, _, _ = self.run_cli("--custom", str(self.ws.root / "missing.json"))
        self.assertEqual(code, errors.EXIT_USAGE)
        self.assert_nothing_sent()

    def test_conflicting_workflow_selections_send_nothing(self):
        self.poscar()
        custom = str(self.custom_file())
        conflicts = (
            ["--relax", "--dos"], ["--dos", "--bands"], ["--relax", "--bands"], ["--dos", "--dos"],
            ["--relax", "--desired-output", "energy"], ["--desired-output", "dos", "--dos"],
            ["--bands", "--custom-workflow", custom], ["--dos", "--custom", custom],
            ["--custom", custom, "--desired-output", "energy"], ["--custom", custom, "--custom-workflow", custom],
            ["--desired-output", "energy", "--custom-workflow", custom],
        )
        for argv in conflicts:
            for explicit in ([], [str(self.ws.files / "Si.POSCAR")]):
                code, out, err = self.run_cli(*(explicit + argv))
                if argv == ["--dos", "--dos"]:
                    # argparse accepts a repeated identical flag; it is still one selection.
                    continue
                self.assertEqual(code, errors.EXIT_USAGE, explicit + argv)
                self.assertEqual(out, "")
        sent = [json.loads(r["body"])["workflow"] for r in self.compute.requests if r["method"] == "POST"]
        self.assertEqual(sent, [{"desired_output": "electronic_dos"}] * 2)
        self.assertEqual(self.ws.records(), {}, "Si has no recorded dos plan, so nothing was prepared")

    def test_shortcut_values_are_not_accepted(self):
        self.poscar()
        for argv in (["--dos=yes"], ["--relax", "--relax=1"]):
            code, _, _ = self.run_cli(*argv)
            self.assertEqual(code, errors.EXIT_USAGE, argv)
        self.assert_nothing_sent()


class Precedence(PoscarBase):
    def test_explicit_file_wins_over_local_poscar(self):
        self.poscar("NiO.POSCAR")
        cases = (([str(self.ws.files / "Si.POSCAR")], SI_TEXT_FIXTURE),
                 ([str(self.ws.files / "Si.cif")], "plan_si_cif_energy_only"),
                 ([str(self.ws.files / "Si.cif"), "--format", "cif"], "plan_si_cif_energy_only"))
        for argv, name in cases:
            before = len(self.compute.requests)
            code, _, err = self.run_cli(*argv)
            self.assertEqual(code, 0, (argv, err))
            self.assertEqual(self.plan_body(before), fixture(name)["request"]["body"])

    def test_explicit_missing_file_never_falls_back_to_poscar(self):
        self.poscar()
        code, _, _ = self.run_cli(str(self.ws.root / "missing.vasp"))
        self.assertEqual(code, errors.EXIT_USAGE)
        self.assert_nothing_sent()

    def test_explicit_poscar_name_is_the_same_file(self):
        self.poscar()
        code, _, _ = self.run_cli("POSCAR")
        self.assertEqual(code, 0)
        self.assertEqual(self.plan_body(), fixture(SI_TEXT_FIXTURE)["request"]["body"])

    def test_flag_like_file_names_need_a_path_or_double_dash(self):
        self.poscar("NiO.POSCAR")
        shutil.copy2(self.ws.files / "Si.POSCAR", self.work / "--dos")
        before = len(self.compute.requests)
        self.run_cli("--dos")  # the shortcut, on ./POSCAR
        self.assertEqual(self.plan_body(before), fixture("plan_nio_poscar_electronic_dos")["request"]["body"])
        before = len(self.compute.requests)
        code, _, err = self.run_cli("./--dos")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.plan_body(before), fixture(SI_TEXT_FIXTURE)["request"]["body"])
        # After "--" everything is a file name, so options must come first.
        environ = {"HOME": str(self.ws.root), "BMD_RUN_STATE_DIR": str(self.ws.state), "BMD_RUN_API_URL": self.server.url}
        before = len(self.compute.requests)
        code = cli.main(["--token-file", str(self.token_file), "--", "--dos"], environ=environ,
                        stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(self.plan_body(before), fixture(SI_TEXT_FIXTURE)["request"]["body"])

    def test_command_named_files_still_need_a_path(self):
        self.poscar()
        shutil.copy2(self.ws.files / "Si.POSCAR", self.work / "status")
        code, out, _ = self.run_cli("--json", "status", run_schema=False)
        self.assertEqual((code, json.loads(out)["schema"]), (errors.EXIT_USAGE, "bmd_run.output"))
        self.assert_nothing_sent()
        self.assertEqual(self.run_cli("./status")[0], 0)


class HelpAndCompatibility(PoscarBase):
    def captured(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(argv, environ={"HOME": str(self.ws.root), "BMD_RUN_STATE_DIR": str(self.ws.state),
                                               "BMD_RUN_API_URL": self.server.url},
                                stdout=out, stderr=err)
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def test_help_and_version_never_run_even_with_poscar(self):
        self.poscar()
        for argv in (["--help"], ["-h"], ["--relax", "--help"], ["--dos", "-h"], ["--json", "--help"],
                     ["POSCAR", "--help"], ["--custom", "x.json", "--help"]):
            code, out, _ = self.captured(argv)
            self.assertEqual(code, 0, argv)
            self.assertIn("usage: bmd-run", out, argv)
        for argv in (["--relax", "--help"], ["--custom", "x.json", "-h"]):  # the one-command help
            self.assertIn("usage: bmd-run [STRUCTURE]", self.captured(argv)[1], argv)
        self.assertNotIn("usage: bmd-run [STRUCTURE]", self.captured(["--help"])[1])
        code, out, _ = self.captured(["POSCAR", "--help"])
        for text in ("--relax", "--dos", "--bands", "--custom FILE", "./POSCAR", "new"):
            self.assertIn(text, out)
        code, out, _ = self.captured(["--version"])
        self.assertEqual((code, out.strip()), (0, f"bmd-run {__version__}"))
        for command in ("identity", "options", "analyze", "plan", "api"):
            code, out, _ = self.captured([command, "--help"])
            self.assertEqual(code, 0, command)
        self.assert_nothing_sent()

    def test_compute_url_without_structure_keeps_legacy_behaviour(self):
        self.poscar()
        code, out, _ = self.run_cli("--json", "--compute-url", "http://127.0.0.1:9", run_schema=False)
        self.assertEqual((code, json.loads(out)["schema"]), (errors.EXIT_USAGE, "bmd_run.output"))
        self.assert_nothing_sent()

    def test_legacy_r1_and_r2_forms_unchanged_with_poscar_present(self):
        from _support import FIXTURES, run_cli

        self.poscar()
        code, _, _, network = run_cli(["plan", "energy", str(FIXTURES / "Si.POSCAR")])
        self.assertEqual((code, network.calls), (0, [("POST", "/build-workflow")]))
        code, _, _, network = run_cli(["identity"])
        self.assertEqual(code, 0)
        environ = {"HOME": str(self.ws.root), "BMD_RUN_STATE_DIR": str(self.ws.state), "BMD_RUN_API_URL": self.server.url}
        out = io.StringIO()
        code = cli.main(["--json", "api", "plan", "POSCAR", "--desired-output", "energy_only",
                         "--token-file", str(self.token_file)], environ=environ, stdout=out, stderr=io.StringIO())
        self.assertEqual((code, json.loads(out.getvalue())["schema"]), (0, "bmd_run.machine_output"))
        self.assertEqual(self.ws.records(), {})
        code, out, _ = self.run_cli(str(self.ws.files / "Si.POSCAR"), "--desired-output", "energy", "--json")
        self.assertEqual((code, json.loads(out)["command"]), (0, "run"))


if __name__ == "__main__":
    import unittest

    unittest.main()
