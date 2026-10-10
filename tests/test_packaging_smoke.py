"""Installed-wheel smoke test for the public package boundary."""

from __future__ import annotations

import configparser
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ENTRY_POINTS = {"console_scripts": {"bmd-run": "bmd_run.cli:entry_point"}}


def _run(args, *, cwd, env):
    return subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def _entry_points_from_text(text: str) -> dict:
    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser.read_string(text)
    return {section: dict(parser.items(section)) for section in parser.sections()}


class InstalledWheelSmoke(unittest.TestCase):
    def test_wheel_installs_and_exposes_only_bmd_run(self):
        with tempfile.TemporaryDirectory(prefix="bmd-run-package-") as tmp_text:
            tmp = Path(tmp_text)
            source = tmp / "source"
            wheelhouse = tmp / "wheelhouse"
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)

            source.mkdir()
            wheelhouse.mkdir()
            for name in ("pyproject.toml", "README.md", "LICENSE"):
                shutil.copy2(ROOT / name, source / name)
            shutil.copytree(
                ROOT / "src",
                source / "src",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
            )

            _run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    "--no-build-isolation",
                    "--wheel-dir",
                    wheelhouse,
                    source,
                ],
                cwd=tmp,
                env=env,
            )
            wheels = sorted(wheelhouse.glob("bmd_run-*.whl"))
            self.assertEqual(len(wheels), 1, [path.name for path in wheelhouse.glob("*.whl")])
            wheel = wheels[0]

            with zipfile.ZipFile(wheel) as archive:
                names = archive.namelist()
            roots = {name.split("/", 1)[0] for name in names}
            dist_info = next(root for root in roots if root.startswith("bmd_run-") and root.endswith(".dist-info"))
            self.assertEqual(roots, {"bmd_run", dist_info})
            self.assertIn("bmd_run/__init__.py", names)
            self.assertIn("bmd_run/schemas/output-v3.schema.json", names)
            self.assertIn("bmd_run/schemas/machine-output-v1.schema.json", names)
            self.assertEqual(
                sorted(name for name in names if name.startswith("bmd_run/")),
                sorted(
                    ["bmd_run/schemas/output-v3.schema.json", "bmd_run/schemas/machine-output-v1.schema.json",
                     "bmd_run/schemas/run-output-v1.schema.json"]
                    + [f"bmd_run/{p.name}" for p in (ROOT / "src" / "bmd_run").glob("*.py")]
                ),
            )
            entry_points_path = f"{dist_info}/entry_points.txt"
            self.assertIn(entry_points_path, names)
            with zipfile.ZipFile(wheel) as archive:
                wheel_entry_points = _entry_points_from_text(archive.read(entry_points_path).decode("utf-8"))
            self.assertEqual(wheel_entry_points, EXPECTED_ENTRY_POINTS)
            legacy_client = "bmd" + "_client"
            legacy_compute_client = "bmd" + "_compute" + "_client"
            forbidden_fragments = (
                "tests/",
                "fixtures/",
                "tools/",
                "ARCHITECTURE.md",
                "CONTRIBUTING.md",
                "SECURITY.md",
                "__pycache__",
                ".git",
                ".env",
                ".log",
                legacy_client,
                legacy_compute_client,
            )
            for fragment in forbidden_fragments:
                self.assertFalse(any(fragment in name for name in names), fragment)

            venv_dir = tmp / "venv"
            venv.EnvBuilder(with_pip=True).create(venv_dir)
            scripts = venv_dir / ("Scripts" if os.name == "nt" else "bin")
            python = scripts / ("python.exe" if os.name == "nt" else "python")

            _run([python, "-m", "pip", "install", "--no-index", wheel], cwd=tmp, env=env)

            metadata_result = _run(
                [
                    python,
                    "-c",
                    (
                        "import importlib.metadata, json; "
                        "eps = {}; "
                        "dist = importlib.metadata.distribution('bmd-run'); "
                        "[eps.setdefault(ep.group, {}).__setitem__(ep.name, ep.value) for ep in dist.entry_points]; "
                        "print(json.dumps(eps, sort_keys=True))"
                    ),
                ],
                cwd=tmp,
                env=env,
            )
            self.assertEqual(json.loads(metadata_result.stdout), EXPECTED_ENTRY_POINTS)

            bmd_run = scripts / ("bmd-run.exe" if os.name == "nt" else "bmd-run")
            self.assertTrue(bmd_run.exists(), sorted(path.name for path in scripts.iterdir()))
            self.assertFalse(list(scripts.glob("bmd-compute*")))

            help_result = _run([bmd_run, "--help"], cwd=tmp, env=env)
            self.assertIn("bmd-run", help_result.stdout)
            self.assertIn("identity", help_result.stdout)
            api_help = _run([bmd_run, "api", "--help"], cwd=tmp, env=env)
            for command in ("plan", "prepare", "submit", "status"):
                self.assertIn(command, api_help.stdout)

            version_result = _run([bmd_run, "--version"], cwd=tmp, env=env)
            self.assertRegex(version_result.stdout, r"^bmd-run 0\.1\.0\s*$")

            import_result = _run(
                [
                    python,
                    "-c",
                    (
                        "import importlib.util, bmd_run; "
                        "print(bmd_run.CLIENT_NAME); "
                        "print(importlib.util.find_spec('bmd' + '_compute' + '_client'))"
                    ),
                ],
                cwd=tmp,
                env=env,
            )
            self.assertEqual(import_result.stdout.splitlines(), ["bmd-run", "None"])


if __name__ == "__main__":
    unittest.main()
