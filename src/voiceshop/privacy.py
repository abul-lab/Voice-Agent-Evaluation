"""Card-number protection for spoken and stored text.

Payment references are spoken with the last four digits only (for example "test card ending 4242"). A full card
number is never spoken back, and it is removed from transcripts before they are stored. Detection is a digit run
of 13 to 19 digits (spaces or dashes allowed between groups) that passes the Luhn check, so order numbers and
phone numbers are not masked by mistake.
"""

from __future__ import annotations

import re

CARD_REMOVED = "[card number removed]"

# 13 to 19 digits, optionally grouped with single spaces or dashes.
_CANDIDATE = re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def find_card_numbers(text: str) -> list[str]:
    """Card-like numbers in the text (digits only, no separators)."""
    found = []
    for match in _CANDIDATE.finditer(text or ""):
        digits = re.sub(r"[ -]", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            found.append(digits)
    return found


def mask_card_numbers(text: str) -> str:
    """Replace every card-like number with a fixed marker. Returns the text unchanged if there is none."""
    if not text:
        return text or ""

    def replace(match: re.Match[str]) -> str:
        digits = re.sub(r"[ -]", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return CARD_REMOVED
        return match.group(0)

    return _CANDIDATE.sub(replace, text)
