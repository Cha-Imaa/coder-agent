"""Discount codes and their fractions."""

DISCOUNTS: dict[str, float] = {"SAVE10": 0.10, "HALF": 0.50}


def discount_for(code: str) -> float:
    """Fraction off for `code`, case-insensitive; ValueError when unknown."""
    try:
        return DISCOUNTS[code.upper()]
    except KeyError:
        raise ValueError(f"unknown discount code: {code!r}") from None
