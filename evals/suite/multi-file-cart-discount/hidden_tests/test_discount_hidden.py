import pytest

from shop.cart import Cart
from shop.discounts import DISCOUNTS, discount_for
from shop.pricing import apply_discount


def _cart() -> Cart:
    c = Cart()
    c.add("book", 10.0, 2)
    c.add("pen", 5.0)
    return c


def test_discount_table():
    assert DISCOUNTS["SAVE10"] == 0.10
    assert DISCOUNTS["HALF"] == 0.50


def test_discount_for_is_case_insensitive():
    assert discount_for("save10") == 0.10
    assert discount_for("Half") == 0.50


def test_discount_for_unknown_raises():
    with pytest.raises(ValueError):
        discount_for("FREE")


def test_apply_discount():
    assert apply_discount(25.0, 0.10) == 22.5
    assert apply_discount(10.0, 0.0) == 10.0


def test_cart_total_with_code_before_tax():
    assert _cart().total(code="SAVE10") == 22.5
    assert _cart().total(tax_rate=0.2, code="SAVE10") == 27.0
    assert _cart().total(tax_rate=0.2, code="HALF") == 15.0


def test_cart_total_without_code_unchanged():
    assert _cart().total(tax_rate=0.2) == 30.0
    assert _cart().total() == 25.0
