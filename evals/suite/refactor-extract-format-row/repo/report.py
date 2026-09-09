"""Plain-text stock reports."""


def render_report(rows: list[tuple[str, int, float]]) -> str:
    """One line per row plus a TOTAL line (sum of qty * price)."""
    lines = []
    for name, qty, price in rows:
        lines.append(f"{name:<12}{qty:>5}{price:>10.2f}")
    total = sum(qty * price for _, qty, price in rows)
    lines.append(f"{'TOTAL':<12}{'':>5}{total:>10.2f}")
    return "\n".join(lines)


def render_summary(rows: list[tuple[str, int, float]]) -> str:
    """Only the rows with stock, under a heading."""
    lines = ["IN STOCK"]
    for name, qty, price in rows:
        if qty > 0:
            lines.append(f"{name:<12}{qty:>5}{price:>10.2f}")
    return "\n".join(lines)
