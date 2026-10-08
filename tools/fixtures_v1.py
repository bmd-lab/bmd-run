#!/usr/bin/env python3
"""Capture, normalise and compare the BMD Compute v1.0.0 test fixtures.

This tool treats BMD Compute as an external HTTP service. It sends requests
only through the client's own closed endpoint table (``Transport`` and
``Endpoint``), so it can reach nothing beyond ``GET /openapi.json``,
``POST /analyze`` and ``POST /build-workflow``. It imports no Compute code.

Reviewer procedure (see tests/fixtures/compute_v1.0.0/RECORDING.md):

    # 1. In a clean BMD Compute checkout at v1.0.0 (a746155), with the pinned
    #    scientific runtime installed, start the service (no SSH needed):
    uvicorn main:app --host 127.0.0.1 --port 8765

    # 2. From this repository, capture fresh responses:
    python tools/fixtures_v1.py capture --compute-url http://127.0.0.1:8765 --out /tmp/fresh

    # 3. Compare them with the committed fixtures after documented normalisation:
    python tools/fixtures_v1.py compare tests/fixtures/compute_v1.0.0 /tmp/fresh

    # Check committed fixtures against MANIFEST.json (no Compute needed):
    python tools/fixtures_v1.py verify

    # After an intentional re-recording, rewrite MANIFEST.json:
    python tools/fixtures_v1.py manifest

Normalisations, applied identically to both sides before comparing. Each one
replaces a value that Compute generates freshly for every request:

    N1  submission identity token (signed, per request)  -> <IDENTITY-TOKEN>
    N2  UUIDs (submission attempt id)                    -> <UUID>
    N3  run timestamps YYYYMMDD-HHMMSS (run/job names)   -> <RUN-TIMESTAMP>
    N4  64-hex SHA-256 of the exact submission script    -> <SHA256>
        (the script embeds N2 and N3, so its hash changes)

Nothing else is normalised. Any other difference is reported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bmd_run.endpoints import Endpoint  # noqa: E402
from bmd_run.errors import ClientError  # noqa: E402
from bmd_run.transport import Transport  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "compute_v1.0.0"
CASES = FIXTURES / "cases.json"
MANIFEST = FIXTURES / "MANIFEST.json"

NORMALISATIONS = (
    ("N1", re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"), "<IDENTITY-TOKEN>"),
    ("N2", re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "<UUID>"),
    ("N3", re.compile(r"(?<![0-9])[0-9]{8}-[0-9]{6}(?![0-9])"), "<RUN-TIMESTAMP>"),
    ("N4", re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])"), "<SHA256>"),
)


def normalise(text: str) -> str:
    for _, pattern, replacement in NORMALISATIONS:
        text = pattern.sub(replacement, text)
    return text


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_cases() -> list:
    return json.loads(CASES.read_text(encoding="utf-8"))["cases"]


def resolve_form(form: dict) -> dict:
    resolved = {}
    for key, value in form.items():
        if isinstance(value, str) and value.startswith("@"):
            value = (FIXTURES / value[1:]).read_text(encoding="utf-8")
        resolved[key] = value
    return resolved


def capture(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    transport = Transport(args.compute_url, timeout=args.timeout)
    failures = 0
    for case in load_cases():
        endpoint = Endpoint[case["endpoint"]]
        response = transport.request(endpoint, resolve_form(case["form"]) or None)
        (out / case["file"]).write_bytes(response.text.encode("utf-8"))
        ok = response.status == case["status"]
        failures += not ok
        print(f"{'ok ' if ok else 'BAD'} {response.status} {case['file']}")
    return 1 if failures else 0


def compare(args) -> int:
    left, right = Path(args.left), Path(args.right)
    differences = 0
    for case in load_cases():
        a = (left / case["file"]).read_bytes()
        b = (right / case["file"]).read_bytes()
        if a == b:
            print(f"identical            {case['file']}")
            continue
        na, nb = normalise(a.decode("utf-8")), normalise(b.decode("utf-8"))
        if na == nb:
            print(f"equal-after-N1..N4   {case['file']}")
            continue
        differences += 1
        line = next(i for i, (x, y) in enumerate(zip(na.splitlines() + [""], nb.splitlines() + [""])) if x != y)
        print(f"DIFFERENT            {case['file']} (first differing line {line + 1})")
    return 1 if differences else 0


def manifest_entries() -> dict:
    entries = {}
    for case in load_cases():
        data = (FIXTURES / case["file"]).read_bytes()
        entries[case["file"]] = {
            "sha256": sha256(data),
            "normalised_sha256": sha256(normalise(data.decode("utf-8")).encode("utf-8")),
        }
    for name in ("Si.POSCAR", "NiO.POSCAR", "cases.json"):
        entries[name] = {"sha256": sha256((FIXTURES / name).read_bytes())}
    return entries


def write_manifest(args) -> int:
    document = {
        "description": "SHA-256 of each committed fixture, raw and after normalisations N1-N4 "
                       "(tools/fixtures_v1.py). Regenerate only after an intentional re-recording.",
        "normalisations": [name for name, _, _ in NORMALISATIONS],
        "files": manifest_entries(),
    }
    MANIFEST.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {MANIFEST.relative_to(ROOT)}")
    return 0


def verify(args) -> int:
    recorded = json.loads(MANIFEST.read_text(encoding="utf-8"))["files"]
    current = manifest_entries()
    bad = sorted(name for name in set(recorded) | set(current) if recorded.get(name) != current.get(name))
    for name in bad:
        print(f"MISMATCH {name}")
    print("manifest ok" if not bad else f"{len(bad)} mismatch(es)")
    return 1 if bad else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("capture", help="request every case from a running Compute v1.0.0")
    p.add_argument("--compute-url", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--timeout", type=float, default=300.0)
    p.set_defaults(func=capture)
    p = commands.add_parser("compare", help="compare two fixture directories after normalisation")
    p.add_argument("left")
    p.add_argument("right")
    p.set_defaults(func=compare)
    commands.add_parser("manifest", help="rewrite MANIFEST.json from committed fixtures").set_defaults(func=write_manifest)
    commands.add_parser("verify", help="check committed fixtures against MANIFEST.json").set_defaults(func=verify)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ClientError as error:
        print(f"error: {error.message}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
