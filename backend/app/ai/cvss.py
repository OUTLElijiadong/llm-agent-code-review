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
    """Normalize a CVSS pair while keeping vector-derived scores authoritative.

    A valid vector is the only deterministic calculation input. When no vector is
    available, a valid explicit model score remains available for backward
    compatibility, but its provenance is marked as ``model``. Missing or invalid
    values remain missing instead of being represented as a zero-risk score.
    """
    normalized_vector = normalize_cvss_vector(vector)
    if normalized_vector is not None:
        calculated = calculate_cvss_score(normalized_vector)
        return calculated, normalized_vector, CVSS_VERSION, "vector"

    normalized_score: Optional[float]
    try:
        normalized_score = round(float(score), 1) if score is not None and score != "" else None
    except (TypeError, ValueError):
        normalized_score = None
    # score-only 只是模型估算，不能伪装成完整的 CVSS v3.1 向量。
    # 0.0 在没有向量时通常表示“未提供”，不要把未知风险展示成零风险。
    if normalized_score is not None and 0.0 < normalized_score <= 10.0:
        return normalized_score, None, None, "model"
    return None, None, None, "unavailable"
