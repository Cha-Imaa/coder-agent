import pytest

from inventory import Inventory


def _stocked() -> Inventory:
    inv = Inventory()
    inv.add("bolt", 5)
    return inv


def test_remove_decrements():
    inv = _stocked()
    inv.remove("bolt", 2)
    assert inv.count("bolt") == 3


def test_remove_to_zero_drops_name():
    inv = _stocked()
    inv.remove("bolt", 5)
    assert inv.count("bolt") == 0
    assert "bolt" not in inv.names()


def test_remove_unknown_raises_key_error():
    with pytest.raises(KeyError):
        _stocked().remove("nut", 1)


def test_remove_too_many_raises_value_error():
    with pytest.raises(ValueError):
        _stocked().remove("bolt", 6)


@pytest.mark.parametrize("qty", [0, -1])
def test_remove_non_positive_raises_value_error(qty):
    with pytest.raises(ValueError):
        _stocked().remove("bolt", qty)
