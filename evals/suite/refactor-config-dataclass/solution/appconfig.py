"""Parse 'key=value' settings text into a Config."""

from dataclasses import dataclass, fields

DEFAULTS = {"host": "localhost", "port": 8080, "debug": False}


def _coerce(key: str, raw):
    if key == "port":
        return int(raw)
    if key == "debug":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    return str(raw).strip()


@dataclass
class Config:
    host: str = DEFAULTS["host"]
    port: int = DEFAULTS["port"]
    debug: bool = DEFAULTS["debug"]

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        """Unknown keys are ignored; missing keys take the defaults."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: _coerce(k, v) for k, v in data.items() if k in known})


def load_config(text: str) -> Config:
    found: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        found[key.strip()] = raw
    return Config.from_dict(found)
