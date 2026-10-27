"""One normaliser and one PO-box cleaner for address text.

Every module used to carry its own copy of these helpers, with subtly different
semantics (punctuation kept vs stripped). Address text now has exactly two
forms here:

``norm``      lowercase, punctuation collapsed to single spaces; for comparison.
``norm_key``  same, but also strips the separators used for cache/lookup keys.
``compact``   lowercase with every non-alphanumeric removed; for substring tests.
"""
import re

ADDRESS_KEYS = ("address_line1", "address_line2", "city", "state",
                "postal_code", "country")

_PO_BOX = re.compile(
    r"(?:\bp\.?\s?o\.?\s*(?:box|bag)\b|"
    r"\bpost\s*(?:box|bag)\b|"
    r"\bp\.?\s?b\.?\s*(?:no|number)?\.?\s*\d+)", re.I)

_NON_WORD = re.compile(r"[^a-z0-9]+")


def norm(value):
    """Lowercase, strip punctuation, collapse whitespace."""
    return " ".join(_NON_WORD.sub(" ", str(value or "").lower()).split())


def norm_key(value):
    """Comparison key that also ignores punctuation differences."""
    return norm(value)


def compact(value):
    """Lowercase with every non-alphanumeric character removed."""
    return _NON_WORD.sub("", str(value or "").lower())


def clean_po_box(value):
    """Drop PO box / post bag segments from one address line."""
    segments = [segment.strip() for segment in str(value or "").split(",")]
    return ", ".join(segment for segment in segments
                     if segment and not _PO_BOX.search(segment))


def clean_address_fields(fields):
    """Return a copy with PO-box segments removed from both address lines."""
    cleaned = dict(fields or {})
    for key in ("address_line1", "address_line2"):
        if key in cleaned:
            cleaned[key] = clean_po_box(cleaned[key])
    return cleaned


def project(fields):
    """The six saveable address columns, empty string for missing values."""
    return {key: str((fields or {}).get(key) or "") for key in ADDRESS_KEYS}
