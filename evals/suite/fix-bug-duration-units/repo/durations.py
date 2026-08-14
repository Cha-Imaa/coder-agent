"""Parse compact duration strings such as '1h30m' into seconds."""

import re

_TOKEN = re.compile(r"(\d+)([hms])")
_UNITS = {"h": 3600, "m": 1, "s": 60}


def parse_duration(text: str) -> int:
    """Return the number of seconds in `text` ('2h', '15m', '1h30m10s')."""
    parts = _TOKEN.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise ValueError(f"not a duration: {text!r}")
    return sum(int(n) * _UNITS[u] for n, u in parts)
