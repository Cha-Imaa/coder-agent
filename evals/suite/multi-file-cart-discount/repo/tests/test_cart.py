from shop.cart import Cart


def _cart() -> Cart:
    c = Cart()
    c.add("book", 10.0, 2)
    c.add("pen", 5.0)
    return c


def test_subtotal():
    assert _cart().subtotal() == 25.0


def test_total_with_tax():
    assert _cart().total(tax_rate=0.2) == 30.0
