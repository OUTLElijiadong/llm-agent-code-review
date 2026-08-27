"""CVSS v3.1 normalization and score calculation."""

from __future__ import annotations

from typing import Optional, Tuple

from cvss import CVSS3
from cvss.exceptions import CVSS3Error

CVSS_VERSION = "3.1"
CVSS_PREFIX = f"CVSS:{CVSS_VERSION}/"


def normalize_cvss_vector(value: object) -> Optional[str]:
    """Return a validated API-compatible CVSS v3.1 vector without its prefix."""
    if value is None:
        return None
    raw = str(value).strip().upper()
    if not raw:
        return None
    prefixed = raw if raw.startswith("CVSS:") else f"{CVSS_PREFIX}{raw}"
    try:
        clean = CVSS3(prefixed).clean_vector()
    except (CVSS3Error, ValueError, TypeError):
        return None
    if not clean.startswith(CVSS_PREFIX):
        return None
    return clean[len(CVSS_PREFIX):]


def calculate_cvss_score(vector: object) -> Optional[float]:
    """Calculate the CVSS v3.1 base score from a validated vector."""
    normalized = normalize_cvss_vector(vector)
    if normalized is None:
        return None
    try:
        return float(CVSS3(f"{CVSS_PREFIX}{normalized}").scores()[0])
    except (CVSS3Error, ValueError, TypeError, IndexError):
        return None


def normalize_cvss(
    score: object,
    vector: object,
) -> Tuple[Optional[float], Optional[str], Optional[str], str]:
    """Normalize CVSS data using a valid v3.1 vector as the only score source.

    ``score`` is accepted for call-site compatibility but is never persisted on
    its own. A model-provided number without a valid vector is not reproducible,
    so it is represented as unavailable instead of being presented as CVSS.
    """
    normalized_vector = normalize_cvss_vector(vector)
    if normalized_vector is not None:
        calculated = calculate_cvss_score(normalized_vector)
        return calculated, normalized_vector, CVSS_VERSION, "vector"
    _ = score
    return None, None, None, "unavailable"
