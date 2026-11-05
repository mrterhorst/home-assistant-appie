"""Parsing of what people type into the to-do list.

Supported input (free text is always the default):

    melk              -> 1x free text "melk"
    2x melk / 2 x melk / 2× melk
    melk x2 / melk x 2
                      -> 2x free text "melk"
    441199            -> 1x AH product 441199 (bare number = webshopId)
    3x 441199         -> 3x AH product 441199

Items with a quantity above 1 are shown as "2x melk", so editing that text
in Home Assistant round-trips through the same parser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .const import MAX_NAME_LENGTH, MAX_QUANTITY

_PREFIX_QTY = re.compile(r"^(\d{1,3})\s*[x×]\s+(.+)$", re.IGNORECASE)
_SUFFIX_QTY = re.compile(r"^(.+?)\s+[x×]\s*(\d{1,3})$", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SPACES = re.compile(r"\s+")
_MAX_PRODUCT_ID_DIGITS = 9


@dataclass(frozen=True)
class ParsedEntry:
    """Result of parsing one to-do line."""

    name: str
    quantity: int
    product_id: int | None = None


def clean_text(text: str | None) -> str:
    """Strip control characters, collapse whitespace and cap the length."""
    if not text:
        return ""
    text = _CONTROL.sub(" ", text)
    text = _SPACES.sub(" ", text).strip()
    return text[:MAX_NAME_LENGTH].rstrip()


def clamp_quantity(quantity: int) -> int:
    return max(1, min(int(quantity), MAX_QUANTITY))


def parse_entry(text: str | None) -> ParsedEntry:
    """Parse a to-do line into name, quantity and an optional product ID."""
    text = clean_text(text)
    quantity = 1
    if match := _PREFIX_QTY.match(text):
        quantity, text = int(match.group(1)), match.group(2).strip()
    elif match := _SUFFIX_QTY.match(text):
        text, quantity = match.group(1).strip(), int(match.group(2))
    quantity = clamp_quantity(quantity)

    if text.isdigit() and len(text) <= _MAX_PRODUCT_ID_DIGITS and int(text) > 0:
        return ParsedEntry(name="", quantity=quantity, product_id=int(text))
    return ParsedEntry(name=text, quantity=quantity)


def format_summary(name: str, quantity: int) -> str:
    """Inverse of parse_entry for display: '2x melk', or just 'melk'."""
    return f"{quantity}x {name}" if quantity > 1 else name
