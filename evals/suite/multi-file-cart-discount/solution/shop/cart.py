"""Cart of priced lines."""

from shop.discounts import discount_for
from shop.pricing import apply_discount, apply_tax, line_total


class Cart:
    def __init__(self) -> None:
        self._lines: list[tuple[str, float, int]] = []

    def add(self, name: str, price: float, qty: int = 1) -> None:
        self._lines.append((name, price, qty))

    def subtotal(self) -> float:
        return round(sum(line_total(p, q) for _, p, q in self._lines), 2)

    def total(self, tax_rate: float = 0.0, code: str | None = None) -> float:
        amount = self.subtotal()
        if code is not None:
            amount = apply_discount(amount, discount_for(code))
        return apply_tax(amount, tax_rate)
