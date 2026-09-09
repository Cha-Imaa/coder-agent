"""Small text helpers."""

import re

_NON_SLUG = re.compile(r"[^a-z0-9]+")


def truncate(text: str, limit: int) -> str:
    """Cut `text` to `limit` characters, ending with an ellipsis when cut."""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "..."


def word_count(text: str) -> int:
    return len(text.split())


def slugify(text: str) -> str:
    """Lower-case URL-safe form: runs of non [a-z0-9] become one hyphen."""
    return _NON_SLUG.sub("-", text.lower()).strip("-")
