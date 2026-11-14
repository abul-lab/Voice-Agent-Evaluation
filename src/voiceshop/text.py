"""Text normalisation shared by the NLU, the monitor and the customer simulator."""

from __future__ import annotations

import re
import unicodedata

_PUNCT = re.compile(r"[^\w:'\-/ ]+")
_SPACE = re.compile(r"\s+")
_SPANISH_ACCENTS = set("ñáéíóúüÑÁÉÍÓÚÜ¿¡")


def fold(text: str) -> str:
    """Lower-case, strip accents and punctuation: 'Mañana, ¿sí?' -> 'manana si'."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _SPACE.sub(" ", _PUNCT.sub(" ", stripped)).strip()


def has_phrase(folded: str, phrase: str) -> bool:
    """Whole-word/phrase containment on folded text (avoids 'hi' matching inside 'this')."""
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", folded) is not None


def spanish_accent_count(text: str) -> int:
    return sum(1 for ch in text if ch in _SPANISH_ACCENTS)


def word_count(text: str) -> int:
    return len(fold(text).split())
