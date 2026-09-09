"""Stock counts by item name."""


class Inventory:
    def __init__(self) -> None:
        self._stock: dict[str, int] = {}

    def add(self, name: str, qty: int) -> None:
        if qty <= 0:
            raise ValueError("qty must be positive")
        self._stock[name] = self._stock.get(name, 0) + qty

    def remove(self, name: str, qty: int) -> None:
        if qty <= 0:
            raise ValueError("qty must be positive")
        if name not in self._stock:
            raise KeyError(name)
        if qty > self._stock[name]:
            raise ValueError(f"only {self._stock[name]} {name} in stock")
        self._stock[name] -= qty
        if self._stock[name] == 0:
            del self._stock[name]

    def count(self, name: str) -> int:
        return self._stock.get(name, 0)

    def names(self) -> list[str]:
        return sorted(self._stock)
