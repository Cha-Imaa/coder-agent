import pytest

from durations import parse_duration


def test_seconds():
    assert parse_duration("45s") == 45


def test_minutes_and_seconds():
    assert parse_duration("1m30s") == 90


def test_all_units():
    assert parse_duration("1h1m1s") == 3661


def test_hours_and_minutes():
    assert parse_duration("1h30m") == 5400


@pytest.mark.parametrize("bad", ["", "5x", "abc", "1h 30m"])
def test_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)
