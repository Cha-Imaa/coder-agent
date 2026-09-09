"""A small named-item registry with tags."""


class Registry:
    def __init__(self) -> None:
        self._tags: dict[str, list[str]] = {}

    def add(self, name: str, tags: list[str] | None = None) -> None:
        """Register `name`; every item is also tagged 'registered'."""
        own = list(tags) if tags else []
        own.append("registered")
        self._tags[name] = own

    def tags(self, name: str) -> list[str]:
        return list(self._tags[name])

    def names(self) -> list[str]:
        return sorted(self._tags)
