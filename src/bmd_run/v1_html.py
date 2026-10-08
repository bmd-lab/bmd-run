"""TRANSITIONAL adapter: reads the HTML pages rendered by BMD Compute v1.0.0.

Compute v1.0.0 has no machine-facing interface, so this module reads the few
facts the read/build proof of concept needs from the browser template
(``templates/index.html`` at commit a746155). It will be replaced by the JSON
``/api/v1`` facade.

The page is untrusted input, and no Compute-originated text is ever relayed.
Every accessor on :class:`Page` returns only:

* client constants and members of the closed vocabularies in :mod:`v1_vocabulary`
  (the client's own copy, never the received string);
* integers and decimals parsed within explicit bounds (:mod:`bounded`);
* INCAR/KPOINTS content rebuilt from validated tokens (:mod:`v1_inputs`).

Prose is not read into output at all: method-consideration text, error
messages, display names, comments and summaries. Where v1 offers no bounded
representation, the result says so explicitly.

Accessors whose names start with ``raw_`` return parsed data for fingerprint
hashing only. Their results must never be placed in output.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from types import MappingProxyType
from typing import Dict, List, Optional

from . import bounded
from . import v1_vocabulary as vocab
from .errors import UnexpectedResponse
from .v1_inputs import parse_previews

_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)

MAX_STAGES = 16
MAX_MODIFIERS = 8
MAX_LIST = 32

# Structure summary labels rendered by Compute v1 (templates/index.html).
STRUCTURE_LABELS = MappingProxyType({
    "formula": "Formula",
    "reduced_formula": "Reduced",
    "natoms": "Atoms",
    "volume": "Volume",
    "density": "Density",
    "a": "a (Å)",
    "b": "b (Å)",
    "c": "c (Å)",
    "alpha": "α (degrees)",
    "beta": "β (degrees)",
    "gamma": "γ (degrees)",
    "space_group_number": "Space Group Number",
    "crystal_system": "Crystal System",
})
# Resources table labels rendered by Compute v1 (Job name deliberately excluded).
RESOURCE_LABELS = (
    ("partition", "Partition"),
    ("account", "Account"),
    ("walltime", "Walltime"),
    ("nodes", "Nodes"),
    ("cpus", "Tasks / CPUs"),
    ("memory", "Memory"),
)


# --- minimal DOM ------------------------------------------------------------------


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: Dict[str, str], parent: Optional["Node"]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: List[object] = []
        self.parent = parent

    @property
    def classes(self) -> List[str]:
        return (self.attrs.get("class") or "").split()

    def iter(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.iter()

    def find_all(self, tag: Optional[str] = None, cls: Optional[str] = None, **attrs: str) -> List["Node"]:
        found = []
        for node in self.iter():
            if tag is not None and node.tag != tag:
                continue
            if cls is not None and cls not in node.classes:
                continue
            if any(node.attrs.get(key) != value for key, value in attrs.items()):
                continue
            found.append(node)
        return found

    def find(self, tag: Optional[str] = None, cls: Optional[str] = None, **attrs: str) -> Optional["Node"]:
        matches = self.find_all(tag, cls, **attrs)
        return matches[0] if matches else None

    def raw_text(self) -> str:
        parts: List[str] = []
        for child in self.children:
            if isinstance(child, Node):
                parts.append("\n" if child.tag == "br" else child.raw_text())
            else:
                parts.append(child)
        return "".join(parts)

    def text(self) -> str:
        """Text with whitespace collapsed; ``<br>`` becomes a line break."""

        lines = [" ".join(line.split()) for line in self.raw_text().split("\n")]
        return "\n".join(line for line in lines if line)


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {}, None)
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {key: (value or "") for key, value in attrs}, self.current)
        self.current.children.append(node)
        if tag not in _VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.current.children.append(Node(tag, {key: (value or "") for key, value in attrs}, self.current))

    def handle_endtag(self, tag):
        node = self.current
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.current = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def parse_document(text: str) -> Node:
    builder = _TreeBuilder()
    builder.feed(text)
    builder.close()
    return builder.root


def _table_rows(node: Node) -> Dict[str, str]:
    rows: Dict[str, str] = {}
    for row in node.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) >= 2:
            rows[cells[0].text()] = cells[1].text()
    return rows


# --- page ------------------------------------------------------------------------


def _unit_value(text, unit: str, field: str) -> float:
    suffix = " " + unit
    if not isinstance(text, str) or not text.endswith(suffix):
        raise bounded.fail(field)
    return bounded.decimal(text[: -len(suffix)], field, low=0, high=1e9)


class Page:
    """One Compute v1 page, read only through bounded parsers."""

    def __init__(self, html_text: str) -> None:
        self.doc = parse_document(html_text)
        self._sections: Dict[str, Node] = {}
        for section in self.doc.find_all("section", cls="stage"):
            heading = section.find("h2")
            if heading is not None:
                self._sections.setdefault(heading.text(), section)

    def _section(self, title: str) -> Node:
        section = self._sections.get(title)
        if section is None:
            raise UnexpectedResponse(f"BMD Compute page has no {title} section.")
        return section

    # --- errors ------------------------------------------------------------------

    def failure_stage(self) -> Optional[str]:
        """``None`` if the page shows no failure; otherwise a client error-stage constant.

        Compute's reason and suggestion prose are not read.
        """

        panels = [node for node in self.doc.find_all("div", cls="prep-panel") if "failed" in node.classes]
        if not panels:
            return None
        title = panels[0].find("div", cls="prep-title")
        stage = vocab.FAILURE_TITLE_STAGE.get(title.text() if title is not None else "")
        return stage if stage is not None else "unrecognized"

    # --- workflow ----------------------------------------------------------------

    def raw_workflow_spec(self) -> dict:
        """Parsed ``workflow_spec_json`` for fingerprint hashing only. Never output."""

        node = self.doc.find("input", name="workflow_spec_json")
        if node is None:
            raise UnexpectedResponse("BMD Compute page has no resolved workflow.")
        try:
            value = json.loads(node.attrs.get("value", ""))
        except json.JSONDecodeError as exc:
            raise UnexpectedResponse("BMD Compute resolved workflow is not valid JSON.") from exc
        if not isinstance(value, dict):
            raise UnexpectedResponse("BMD Compute resolved workflow has an unexpected shape.")
        return value

    @staticmethod
    def _stages(stages, field: str) -> List[dict]:
        if not isinstance(stages, list) or not 1 <= len(stages) <= MAX_STAGES:
            raise bounded.fail(field)
        projected = []
        for index, stage in enumerate(stages, start=1):
            if not isinstance(stage, dict):
                raise bounded.fail(field)
            modifiers = stage.get("modifiers") or []
            if not isinstance(modifiers, list) or len(modifiers) > MAX_MODIFIERS:
                raise bounded.fail(f"{field}.modifiers")
            stage_type = bounded.choice(stage.get("stage_type"), vocab.STAGE_TYPES, f"{field}.stage_type")
            theory = bounded.choice(stage.get("theory"), vocab.THEORIES, f"{field}.theory")
            mods = [bounded.choice(m, vocab.MODIFIERS, f"{field}.modifiers") for m in modifiers]
            projected.append({
                "index": index,
                "stage_type": stage_type,
                "theory": theory,
                "modifiers": mods,
                "label": vocab.stage_label(stage_type, theory, mods),
            })
        return projected

    def workflow(self) -> dict:
        """Desired Output and stages as vocabulary members. Stage options and all other keys are ignored."""

        spec = self.raw_workflow_spec()
        return {
            "desired_output": bounded.choice(spec.get("recipe"), vocab.DESIRED_OUTPUTS, "workflow.desired_output"),
            "stages": self._stages(spec.get("stages"), "workflow.stages"),
        }

    # --- option catalogue ----------------------------------------------------------

    def raw_option_catalogue(self) -> dict:
        """Parsed option catalogue for fingerprint hashing only. Never output."""

        node = self.doc.find("script", id="calculation-stage-options")
        if node is None:
            raise UnexpectedResponse("BMD Compute page has no option catalogue.")
        try:
            value = json.loads(node.raw_text())
        except json.JSONDecodeError as exc:
            raise UnexpectedResponse("BMD Compute option catalogue is not valid JSON.") from exc
        if not isinstance(value, dict):
            raise UnexpectedResponse("BMD Compute option catalogue has an unexpected shape.")
        return value

    def option_catalogue(self) -> dict:
        raw = self.raw_option_catalogue()
        unrecognized = 0

        def items(key: str) -> list:
            value = raw.get(key)
            if value is None:
                return []
            if not isinstance(value, list) or len(value) > MAX_LIST:
                raise bounded.fail(f"options.{key}")
            return [item for item in value if isinstance(item, dict)]

        desired = []
        for item in items("desired_outputs"):
            value = bounded.optional_choice(item.get("value"), vocab.DESIRED_OUTPUTS)
            if value is None:
                unrecognized += 1
                continue
            spec = item.get("workflow_spec")
            try:
                if spec is None:
                    stages = []
                elif isinstance(spec, dict):
                    stages = self._stages(spec.get("stages"), "options.desired_outputs.stages")
                else:
                    raise bounded.fail("options.desired_outputs.workflow_spec")
            except bounded.ComputeFormatError:
                unrecognized += 1
                continue
            desired.append({"value": value, "label": vocab.DESIRED_OUTPUT_LABELS[value], "default_stages": stages})

        def simple(key: str, options, labels, with_enabled: bool) -> list:
            nonlocal unrecognized
            out = []
            for item in items(key):
                value = bounded.optional_choice(item.get("value"), options)
                if value is None:
                    unrecognized += 1
                    continue
                entry = {"value": value, "label": labels[value]}
                if with_enabled:
                    enabled = item.get("enabled")
                    entry["enabled"] = enabled if isinstance(enabled, bool) else None
                out.append(entry)
            return out

        return {
            "desired_outputs": desired,
            "stage_types": simple("stage_types", vocab.STAGE_TYPES, vocab.STAGE_TYPE_LABELS, False),
            "theories": simple("theories", vocab.THEORIES, vocab.THEORY_LABELS, True),
            "modifiers": simple("modifiers", vocab.MODIFIERS, vocab.MODIFIER_LABELS, True),
            "unrecognized_entry_count": unrecognized,
        }

    # --- structure facts and considerations -----------------------------------------

    def structure(self) -> dict:
        section = self._section("Structure Summary")
        rows: Dict[str, str] = {}
        for metric in section.find_all("div", cls="metric"):
            label = metric.find("div", cls="metric-label")
            value = metric.find("div", cls="metric-value")
            if label is not None and value is not None:
                rows[label.text()] = value.text()
        for disclosure in section.find_all("details", cls="disclosure"):
            rows.update(_table_rows(disclosure))

        def row(key: str):
            return rows.get(STRUCTURE_LABELS[key])

        formula = bounded.formula(row("formula"), "structure.formula")
        reduced = bounded.formula(row("reduced_formula"), "structure.reduced_formula")
        space_group = row("space_group_number")
        crystal_system = row("crystal_system")
        return {
            "formula": bounded.render_formula(formula, reduced=False),
            "reduced_formula": bounded.render_formula(reduced, reduced=True),
            "composition": [{"element": e, "count": int(c) if float(c).is_integer() else c} for e, c in formula],
            "natoms": bounded.integer(row("natoms"), "structure.natoms", low=1, high=10**6),
            "volume_angstrom3": _unit_value(row("volume"), "Å³", "structure.volume"),
            "density_g_cm3": _unit_value(row("density"), "g/cm³", "structure.density"),
            "lattice": {
                key: bounded.decimal(row(key), f"structure.{key}", low=0, high=1e6)
                for key in ("a", "b", "c", "alpha", "beta", "gamma")
            },
            "space_group_number": (
                None if space_group in (None, "Unknown")
                else bounded.integer(space_group, "structure.space_group_number", low=1, high=230)
            ),
            "crystal_system": (
                None if crystal_system in (None, "Unknown")
                else bounded.choice(crystal_system, vocab.CRYSTAL_SYSTEMS, "structure.crystal_system")
            ),
        }

    def considerations(self) -> List[dict]:
        cards = self.doc.find_all("div", cls="method-consideration-card")
        if len(cards) > MAX_LIST:
            raise bounded.fail("method_considerations")
        projected = []
        for card in cards:
            consideration_id = bounded.optional_choice(card.attrs.get("data-method-consideration-id"),
                                                       vocab.CONSIDERATION_IDS)
            title_node = card.find("div", cls="method-consideration-title")
            title = None
            if title_node is not None:
                spans = [span for span in title_node.find_all("span") if "step-mark" not in span.classes]
                title = " ".join(span.text() for span in spans)
            status = vocab.CONSIDERATION_TITLE_STATUS.get((consideration_id, title), "unrecognized")
            projected.append({
                "id": consideration_id,
                "label": vocab.CONSIDERATION_LABELS[consideration_id] if consideration_id else None,
                "status": status,
            })
        return projected

    # --- build page ---------------------------------------------------------------

    def _summary_table(self, heading_text: str) -> Dict[str, str]:
        section = self._section("Generated Inputs")
        for block in section.find_all("section", cls="submission-summary-section"):
            heading = block.find("h3")
            if heading is not None and heading.text() == heading_text:
                return _table_rows(block)
        return {}

    def stage_executables(self, stage_count: int) -> Dict[int, Optional[str]]:
        """VASP executable per stage from the Execution table; all other rows are ignored."""

        rows = self._summary_table("Execution")
        out: Dict[int, Optional[str]] = {}
        for index in range(1, stage_count + 1):
            value = rows.get(f"Stage {index} VASP")
            out[index] = None if value is None else bounded.choice(
                value, vocab.VASP_EXECUTABLES, f"stages[{index}].vasp_executable")
        return out

    def raw_resource_rows(self) -> Dict[str, Optional[str]]:
        """Resources table values for fingerprint hashing only. Never output."""

        rows = self._summary_table("Resources")
        if not rows:
            raise UnexpectedResponse("BMD Compute page has no Resources summary.")
        return {key: rows.get(label) for key, label in RESOURCE_LABELS}

    def resources(self) -> dict:
        rows = self.raw_resource_rows()
        memory = rows["memory"]
        if not isinstance(memory, str) or not memory.endswith(" GB"):
            raise bounded.fail("resources.memory")
        walltime = rows["walltime"]
        parts = walltime.split(":") if isinstance(walltime, str) else []
        if len(parts) != 3:
            raise bounded.fail("resources.walltime")
        hours = bounded.integer(parts[0], "resources.walltime", low=0, high=999)
        minutes = bounded.integer(parts[1], "resources.walltime", low=0, high=59)
        seconds = bounded.integer(parts[2], "resources.walltime", low=0, high=59)
        return {
            "partition": bounded.optional_choice(rows["partition"], vocab.PARTITIONS),
            "account": bounded.optional_choice(rows["account"], vocab.ACCOUNTS),
            "cpus": bounded.integer(rows["cpus"], "resources.cpus", low=1, high=10**6),
            "nodes": bounded.integer(rows["nodes"], "resources.nodes", low=1, high=10**4),
            "memory_gb": bounded.integer(memory[:-3], "resources.memory", low=1, high=10**6),
            "walltime": f"{hours:02d}:{minutes:02d}:{seconds:02d}",
        }

    def raw_input_previews(self) -> Dict[str, Optional[str]]:
        """INCAR/KPOINTS/POSCAR preview text, unvalidated (fingerprint hashing only)."""

        section = self._section("Generated Inputs")

        def panel_text(panel_class: str) -> Optional[str]:
            panel = section.find("div", cls=panel_class)
            pre = panel.find("pre") if panel is not None else None
            return pre.raw_text() if pre is not None else None

        previews = {
            "incar": panel_text("tab-panel-incar"),
            "kpoints": panel_text("tab-panel-kpoints"),
            "poscar": panel_text("tab-panel-poscar"),
        }
        if previews["incar"] is None:
            raise UnexpectedResponse("BMD Compute page has no INCAR preview.")
        return previews

    def input_stages(self, stage_count: int) -> List[dict]:
        previews = self.raw_input_previews()
        return parse_previews(previews["incar"], previews["kpoints"], stage_count)
