from appconfig import load_config


def test_defaults():
    cfg = load_config("")
    assert cfg["host"] == "localhost"
    assert cfg["port"] == 8080
    assert cfg["debug"] is False


def test_parses_and_coerces():
    cfg = load_config("# comment\nport = 9000\ndebug=yes\nhost=example.org\nignored=1")
    assert cfg["port"] == 9000
    assert cfg["debug"] is True
    assert cfg["host"] == "example.org"
