import pytest

from stack import Stack


def test_push_pop_order():
    s = Stack()
    s.push(1)
    s.push(2)
    assert s.pop() == 2
    assert s.pop() == 1
    assert s.is_empty()


def test_pop_empty_raises():
    with pytest.raises(IndexError):
        Stack().pop()
