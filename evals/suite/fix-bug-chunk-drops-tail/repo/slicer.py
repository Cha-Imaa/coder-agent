"""Split sequences into fixed-size pieces."""


def chunk(items: list, size: int) -> list[list]:
    """Return consecutive slices of `items`, each with at most `size` elements."""
    if size <= 0:
        raise ValueError("size must be positive")
    return [items[i : i + size] for i in range(0, len(items) - size + 1, size)]
