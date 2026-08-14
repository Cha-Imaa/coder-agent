"""Money arithmetic, rounded to cents at the edges."""


def line_total(price: float, qty: int) -> float:
    return round(price * qty, 2)


def apply_discount(amount: float, fraction: float) -> float:
    return round(amount * (1 - fraction), 2)


def apply_tax(amount: float, rate: float) -> float:
    return round(amount * (1 + rate), 2)
