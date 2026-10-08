"""Bounded parsers for Compute v1's INCAR and KPOINTS previews.

These parsers read only the syntax Compute emitted and rebuild a
Client-owned representation from validated tokens:

* INCAR: ``TAG = value`` lines. Tags must be in the closed VASP tag vocabulary
  (:mod:`incar_tags`). Values are parsed by syntactic type into numbers,
  logicals, or words from that tag's closed word list.
  - Tags outside the vocabulary, and free-text tags such as ``SYSTEM``, are
    withheld: counted, never reported.
  - Any line that is not ``TAG = value`` makes the response unusable.
* KPOINTS: automatic meshes become mode plus subdivisions (and shift).
  Explicit lists become a count and coordinate mode; the coordinates and
  labels are not reported. Comment lines are never read into output.
* Stage headers (``# Stage N - ...``) are used only for their number and VASP
  executable. Their descriptive text is never reported.

The parsers make no scientific decision and validate no methodology. A value
is checked only for its syntactic type and its size.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from . import bounded
from . import v1_vocabulary as vocab
from .incar_tags import TAGS

MAX_INCAR_LINES = 400
MAX_LIST_TOKENS = 4096
MAX_EXPLICIT_KPOINTS = 100000

_HEADER = re.compile(r"^# Stage ([0-9]{1,2}) - ")
_EXECUTABLE = re.compile(r"^# VASP executable - (\S+)$")
_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]{0,31})\s*=\s*(\S.*)$")
_REPEAT = re.compile(r"^([0-9]{1,6})\*(\S+)$")
_TRUE = ("true", ".true.", "t", ".t.")
_FALSE = ("false", ".false.", "f", ".f.")
_CANONICAL_TAG = {tag: tag for tag in TAGS}


def split_stages(text: str, stage_count: int, field: str) -> List[Tuple[int, Optional[str], List[str]]]:
    """Split a preview into ``(index, executable, body_lines)`` per stage."""

    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    if len(lines) > MAX_EXPLICIT_KPOINTS + 50 * max(stage_count, 1):
        raise bounded.fail(field)
    if stage_count == 1 and not any(_HEADER.match(line) for line in lines):
        return [(1, None, [line for line in lines if line])]

    blocks: List[Tuple[int, Optional[str], List[str]]] = []
    current: Optional[list] = None
    for line in lines:
        header = _HEADER.match(line)
        if header:
            index = int(header.group(1))
            if index != len(blocks) + 1:
                raise bounded.fail(f"{field}.stage_header")
            current = [index, None, []]
            blocks.append(current)  # type: ignore[arg-type]
            continue
        if current is None:
            if line:
                raise bounded.fail(f"{field}.preamble")
            continue
        executable = _EXECUTABLE.match(line)
        if executable and not current[2] and current[1] is None:
            current[1] = bounded.choice(executable.group(1), vocab.VASP_EXECUTABLES, f"{field}.executable")
            continue
        if line:
            current[2].append(line)
    if len(blocks) != stage_count:
        raise bounded.fail(f"{field}.stage_count")
    return [(index, executable, body) for index, executable, body in blocks]


# --- INCAR ---------------------------------------------------------------------


def _logical(token: str) -> Optional[bool]:
    folded = token.casefold()
    if folded in _TRUE:
        return True
    if folded in _FALSE:
        return False
    return None


def _scalar(token: str, field: str):
    as_bool = _logical(token)
    if as_bool is not None:
        return as_bool
    if re.match(r"^[+-]?[0-9]{1,9}$", token):
        return int(token)
    return bounded.decimal(token, field)


def parse_incar(lines: List[str], field: str) -> dict:
    if len(lines) > MAX_INCAR_LINES:
        raise bounded.fail(field)
    tags: List[dict] = []
    seen = set()
    withheld = 0
    for number, line in enumerate(lines, start=1):
        match = _ASSIGNMENT.match(line)
        if not match:
            raise bounded.fail(f"{field}.line")
        tag, raw = match.group(1), match.group(2).strip()
        if tag in seen:
            raise bounded.fail(f"{field}.duplicate_tag")
        seen.add(tag)
        spec = TAGS.get(tag)
        if spec is None or spec[0] == "opaque":
            withheld += 1
            continue
        kind, words = spec
        tokens = raw.split()
        label = f"{field}.value"
        if kind == "list":
            if len(tokens) > MAX_LIST_TOKENS:
                raise bounded.fail(label)
            value = []
            for token in tokens:
                repeat = _REPEAT.match(token)
                if repeat:
                    value.append({"repeat": bounded.integer(repeat.group(1), label, low=1, high=10**6),
                                  "value": _scalar(repeat.group(2), label)})
                else:
                    value.append({"repeat": None, "value": _scalar(token, label)})
        else:
            if len(tokens) != 1:
                raise bounded.fail(label)
            token = tokens[0]
            if kind == "bool":
                value = _logical(token)
                if value is None:
                    raise bounded.fail(label)
            elif kind == "int":
                value = bounded.integer(token, label, low=-10**9, high=10**9)
            elif kind == "float":
                value = bounded.decimal(token, label)
            elif kind == "word":
                try:
                    value = bounded.word(token, words, label)
                except bounded.ComputeFormatError:
                    withheld += 1  # a word outside the closed list is withheld, not reported
                    continue
            else:  # bool_or_word
                value = _logical(token)
                if value is None:
                    try:
                        value = bounded.word(token, words, label)
                    except bounded.ComputeFormatError:
                        withheld += 1
                        continue
        tags.append({"tag": _CANONICAL_TAG[tag], "value": value})
    return {"tags": tags, "withheld_tag_count": withheld, "text": render_incar(tags)}


def _render_scalar(value) -> str:
    return bounded.render_number(value) if not isinstance(value, str) else value


def render_incar(tags: List[dict]) -> str:
    lines = []
    for item in tags:
        value = item["value"]
        if isinstance(value, list):
            text = " ".join(
                (f"{entry['repeat']}*" if entry["repeat"] is not None else "") + _render_scalar(entry["value"])
                for entry in value
            )
        else:
            text = _render_scalar(value)
        lines.append(f"{item['tag']} = {text}")
    return "\n".join(lines)


# --- KPOINTS ---------------------------------------------------------------------


def parse_kpoints(lines: List[str], field: str) -> dict:
    """Parse one stage's KPOINTS body (first line is a comment and is ignored)."""

    if len(lines) < 4:
        raise bounded.fail(field)
    count = bounded.integer(lines[1].strip(), f"{field}.count", low=0, high=MAX_EXPLICIT_KPOINTS)
    third = lines[2].strip().casefold()
    if count == 0:
        mode = vocab.KPOINT_MESH_MODES.get(third)
        if mode is None:
            raise bounded.fail(f"{field}.mode")
        subdivisions = [bounded.integer(t, f"{field}.subdivisions", low=1, high=1000) for t in lines[3].split()]
        if len(subdivisions) != 3:
            raise bounded.fail(f"{field}.subdivisions")
        shift = None
        if len(lines) == 5:
            shift = [bounded.decimal(t, f"{field}.shift", low=-1, high=1) for t in lines[4].split()]
            if len(shift) != 3:
                raise bounded.fail(f"{field}.shift")
        elif len(lines) != 4:
            raise bounded.fail(field)
        return {"kind": "automatic_mesh", "mode": mode, "subdivisions": subdivisions, "shift": shift}

    coordinate_mode = vocab.KPOINT_COORDINATE_MODES.get(third)
    if coordinate_mode is None or len(lines) != 3 + count:
        raise bounded.fail(f"{field}.explicit")
    for line in lines[3:]:
        tokens = line.split()
        if len(tokens) < 4:
            raise bounded.fail(f"{field}.explicit")
        for token in tokens[:4]:  # checked for shape only; never reported
            if not bounded.is_number(token):
                raise bounded.fail(f"{field}.explicit")
    return {"kind": "explicit_list", "count": count, "coordinate_mode": coordinate_mode}


def render_kpoints(kpoints: dict) -> str:
    if kpoints["kind"] == "automatic_mesh":
        lines = ["Automatic mesh", "0", kpoints["mode"], " ".join(str(n) for n in kpoints["subdivisions"])]
        if kpoints["shift"] is not None:
            lines.append(" ".join(bounded.render_number(x) for x in kpoints["shift"]))
        return "\n".join(lines)
    return f"Explicit k-point list: {kpoints['count']} points ({kpoints['coordinate_mode']}); coordinates not shown"


def parse_previews(incar_text: str, kpoints_text: Optional[str], stage_count: int) -> List[dict]:
    incar_blocks = split_stages(incar_text, stage_count, "generated_inputs.incar")
    kpoint_blocks = split_stages(kpoints_text, stage_count, "generated_inputs.kpoints") if kpoints_text else None
    stages: List[dict] = []
    for position, (index, executable, body) in enumerate(incar_blocks):
        incar = parse_incar(body, f"generated_inputs.stages[{index}].incar")
        kpoints = None
        if kpoint_blocks is not None:
            k_index, k_executable, k_body = kpoint_blocks[position]
            if k_executable != executable:
                raise bounded.fail(f"generated_inputs.stages[{index}].executable")
            kpoints = parse_kpoints(k_body, f"generated_inputs.stages[{index}].kpoints")
        stages.append({
            "index": index,
            "header_vasp_executable": executable,
            "incar": incar,
            "kpoints": kpoints,
        })
    return stages
