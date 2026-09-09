import pytest

from stack import Stack


def test_peek_returns_top_without_removing():
    s = Stack()
    s.push("a")
    s.push("b")
    assert s.peek() == "b"
    assert s.peek() == "b"
    assert len(s) == 2


def test_peek_empty_raises_index_error():
    with pytest.raises(IndexError):
        Stack().peek()


def test_len_tracks_push_and_pop():
    s = Stack()
    assert len(s) == 0
    s.push(1)
    s.push(2)
    assert len(s) == 2
    s.pop()
    assert len(s) == 1
