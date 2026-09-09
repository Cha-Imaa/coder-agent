"""Plain-text stock reports."""


def format_row(name: str, qty: int, price: float) -> str:
    """Fixed-width row: 12-char name, 5-char qty, 10-char price with two decimals."""
    return f"{name:<12}{qty:>5}{price:>10.2f}"


def render_report(rows: list[tuple[str, int, float]]) -> str:
    """One line per row plus a TOTAL line (sum of qty * price)."""
    lines = [format_row(name, qty, price) for name, qty, price in rows]
    total = sum(qty * price for _, qty, price in rows)
    lines.append(f"{'TOTAL':<12}{'':>5}{total:>10.2f}")
    return "\n".join(lines)


def render_summary(rows: list[tuple[str, int, float]]) -> str:
    """Only the rows with stock, under a heading."""
    lines = ["IN STOCK"]
    lines.extend(format_row(name, qty, price) for name, qty, price in rows if qty > 0)
    return "\n".join(lines)
