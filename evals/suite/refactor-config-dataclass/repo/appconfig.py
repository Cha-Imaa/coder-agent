"""Parse 'key=value' settings text into a settings mapping."""

DEFAULTS = {"host": "localhost", "port": 8080, "debug": False}


def _coerce(key: str, raw: str):
    if key == "port":
        return int(raw)
    if key == "debug":
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return raw.strip()


def load_config(text: str) -> dict:
    """Unknown keys are ignored; missing keys take DEFAULTS."""
    found: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        if key in DEFAULTS:
            found[key] = _coerce(key, raw)
    return {**DEFAULTS, **found}
