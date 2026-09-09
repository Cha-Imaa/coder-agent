import pytest

from inventory import Inventory


def test_add_accumulates():
    inv = Inventory()
    inv.add("bolt", 3)
    inv.add("bolt", 2)
    assert inv.count("bolt") == 5


def test_add_rejects_non_positive():
    with pytest.raises(ValueError):
        Inventory().add("bolt", 0)


def test_unknown_count_is_zero():
    assert Inventory().count("nut") == 0
