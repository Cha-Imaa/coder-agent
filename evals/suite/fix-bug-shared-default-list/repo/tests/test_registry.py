from registry import Registry


def test_add_and_list():
    r = Registry()
    r.add("b")
    r.add("a")
    assert r.names() == ["a", "b"]


def test_items_do_not_share_tags():
    r = Registry()
    r.add("first")
    r.add("second")
    assert r.tags("first") == ["registered"]
    assert r.tags("second") == ["registered"]
