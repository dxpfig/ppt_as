"""Numeric-token extraction for source-fidelity checks."""
from __future__ import annotations

import re


NUMBER_TOKEN_PATTERN = (
    r"(?<![A-Za-z])\d+(?:\.\d+)?"
    r"(?:%|万亿|亿|万|TOPS|G|年|项|类|个|所|台)?"
)
_GROUPED_INSTITUTE_IDS = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(\d+(?:\.\d+)?(?:\s*[/、,，和]\s*\d+(?:\.\d+)?)+)"
    r"\s*(所)"
)
_GROUP_SEPARATOR = re.compile(r"\s*[/、,，和]\s*")


def expand_grouped_units(text: str) -> str:
    """Expand lists whose shared unit belongs to every number.

    ``电科15/32/52所`` is a compact, faithful way to write ``15所、32所、
    52所``. Expanding only recognised units preserves strict checks for values
    such as ``709亿元`` or ``709 TOPS``.
    """

    def expand(match: re.Match) -> str:
        values = _GROUP_SEPARATOR.split(match.group(1))
        unit = match.group(2)
        return " ".join(f"{value}{unit}" for value in values)

    return _GROUPED_INSTITUTE_IDS.sub(expand, str(text or ""))


def numeric_tokens(text: str) -> list[str]:
    """Return stable numeric tokens after expanding grouped institute IDs."""
    expanded = expand_grouped_units(text)
    return sorted(set(re.findall(NUMBER_TOKEN_PATTERN, expanded)))
