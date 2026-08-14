"""Small text helpers."""


def truncate(text: str, limit: int) -> str:
    """Cut `text` to `limit` characters, ending with an ellipsis when cut."""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "..."


def word_count(text: str) -> int:
    return len(text.split())
