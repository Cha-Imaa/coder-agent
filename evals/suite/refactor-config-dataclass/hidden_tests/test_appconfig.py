import dataclasses

from appconfig import Config, load_config


def test_config_is_a_dataclass_with_expected_fields():
    assert dataclasses.is_dataclass(Config)
    assert [f.name for f in dataclasses.fields(Config)] == ["host", "port", "debug"]


def test_defaults():
    cfg = load_config("")
    assert isinstance(cfg, Config)
    assert (cfg.host, cfg.port, cfg.debug) == ("localhost", 8080, False)


def test_parses_and_coerces():
    cfg = load_config("# comment\nport = 9000\ndebug=yes\nhost=example.org\nignored=1")
    assert (cfg.host, cfg.port, cfg.debug) == ("example.org", 9000, True)


def test_from_dict_applies_defaults_and_coercion():
    cfg = Config.from_dict({"port": "1", "debug": "off"})
    assert cfg == Config(host="localhost", port=1, debug=False)
