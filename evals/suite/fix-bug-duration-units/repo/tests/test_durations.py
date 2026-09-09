from durations import parse_duration


def test_hours():
    assert parse_duration("2h") == 7200


def test_minutes():
    assert parse_duration("2m") == 120
