"""Sanitize internal-only review execution metadata before returning coverage."""


def public_review_coverage(coverage) -> dict | None:
    """Return a shallow display copy without private billing/authorization fields."""
    if not isinstance(coverage, dict):
        return None
    result = dict(coverage)
    result.pop("_billing_subject", None)
    return result
