from registry import Registry


def test_independent_default_tags():
    r = Registry()
    r.add("x")
    r.add("y")
    r.add("z")
    assert r.tags("x") == ["registered"]
    assert r.tags("z") == ["registered"]


def test_explicit_tags_are_kept():
    r = Registry()
    r.add("x", ["blue"])
    assert sorted(r.tags("x")) == ["blue", "registered"]


def test_callers_list_is_not_mutated():
    mine = ["blue"]
    r = Registry()
    r.add("x", mine)
    assert mine == ["blue"]


def test_two_explicit_lists_stay_separate():
    r = Registry()
    r.add("x", ["one"])
    r.add("y", ["two"])
    assert "two" not in r.tags("x")
