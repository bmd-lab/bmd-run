"""Bounded parsers for values read from Compute pages.

Every function here takes a string from an untrusted Compute response and
returns one of:

* a client-owned constant (the matching member of a closed vocabulary, not the
  received string);
* a Python ``int`` or ``float`` within explicit range and precision bounds.

Anything else raises :class:`ComputeFormatError`. Error messages name the
client's own field label and never include the received value.
"""

from __future__ import annotations

import math
import re
from typing import Optional, Sequence, Tuple

from . import v1_vocabulary as vocab
from .errors import UnexpectedResponse

MAX_SIGNIFICANT_DIGITS = 12
MAX_ABS_EXPONENT = 99

_INT = re.compile(r"^[+-]?[0-9]{1,9}$")
_DECIMAL = re.compile(r"^([+-]?)([0-9]*)(?:\.([0-9]*))?(?:[eE]([+-]?[0-9]{1,3}))?$")
_FORMULA_TERM = re.compile(r"([A-Z][a-z]?)([0-9]+(?:\.[0-9]+)?)?")


class ComputeFormatError(UnexpectedResponse):
    """A Compute response value does not have the v1.0.0 format the client parses."""


def fail(field: str) -> ComputeFormatError:
    return ComputeFormatError(
        f"BMD Compute response field '{field}' does not have the BMD Compute v1.0.0 format "
        "the client parses. Nothing from this response was reported.",
        suggestion="The service is not answering like BMD Compute v1.0.0.",
    )


def choice(value, options: Sequence[str], field: str) -> str:
    """Return the client's own member of ``options`` equal to ``value``."""

    for option in options:
        if value == option:
            return option
    raise fail(field)


def optional_choice(value, options: Sequence[str]) -> Optional[str]:
    for option in options:
        if value == option:
            return option
    return None


def word(value: str, options: Sequence[str], field: str) -> str:
    """Case-insensitive match; returns the client's canonical spelling."""

    if isinstance(value, str):
        folded = value.casefold()
        for option in options:
            if option.casefold() == folded:
                return option
    raise fail(field)


def integer(value, field: str, *, low: int, high: int) -> int:
    if not isinstance(value, str) or not _INT.match(value):
        raise fail(field)
    number = int(value)
    if not low <= number <= high:
        raise fail(field)
    return number


def decimal(value, field: str, *, low: float = -1e12, high: float = 1e12) -> float:
    """Parse a decimal with at most 12 significant digits into a float within bounds."""

    if not isinstance(value, str) or len(value) > 40:
        raise fail(field)
    match = _DECIMAL.match(value)
    if not match or not (match.group(2) or match.group(3)):
        raise fail(field)
    digits = ((match.group(2) or "") + (match.group(3) or "")).lstrip("0").rstrip("0")
    if len(digits) > MAX_SIGNIFICANT_DIGITS:
        raise fail(field)
    if match.group(4) is not None and abs(int(match.group(4))) > MAX_ABS_EXPONENT:
        raise fail(field)
    number = float(value)
    if not math.isfinite(number) or not low <= number <= high:
        raise fail(field)
    return number


_LOOSE_NUMBER = re.compile(r"^[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?$")


def is_number(value) -> bool:
    """Shape check for numbers the client parses but never reports."""

    return isinstance(value, str) and len(value) <= 40 and bool(_LOOSE_NUMBER.match(value))


def render_number(number) -> str:
    """Client-side rendering of a parsed number."""

    if isinstance(number, bool):
        return "True" if number else "False"
    if isinstance(number, int):
        return str(number)
    return repr(float(number))


def formula(value, field: str) -> Tuple[Tuple[str, float], ...]:
    """Parse a chemical formula such as ``Ni1 O1`` or ``NiO`` into (element, count) pairs."""

    if not isinstance(value, str) or not 0 < len(value) <= 200:
        raise fail(field)
    compact = value.replace(" ", "")
    terms = _FORMULA_TERM.findall(compact)
    if not terms or "".join(e + c for e, c in terms) != compact or len(terms) > 20:
        raise fail(field)
    parsed = []
    for element, count in terms:
        symbol = choice(element, vocab.ELEMENTS, field)
        amount = decimal(count, field, low=0, high=1e6) if count else 1.0
        parsed.append((symbol, amount))
    return tuple(parsed)


def render_formula(terms, *, reduced: bool) -> str:
    def count_text(count: float) -> str:
        return str(int(count)) if float(count).is_integer() else repr(float(count))

    if reduced:
        return "".join(e + ("" if c == 1 else count_text(c)) for e, c in terms)
    return " ".join(e + count_text(c) for e, c in terms)
