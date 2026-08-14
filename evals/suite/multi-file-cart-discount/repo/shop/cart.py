"""Cart of priced lines."""

from shop.pricing import apply_tax, line_total


class Cart:
    def __init__(self) -> None:
        self._lines: list[tuple[str, float, int]] = []

    def add(self, name: str, price: float, qty: int = 1) -> None:
        self._lines.append((name, price, qty))

    def subtotal(self) -> float:
        return round(sum(line_total(p, q) for _, p, q in self._lines), 2)

    def total(self, tax_rate: float = 0.0) -> float:
        return apply_tax(self.subtotal(), tax_rate)
