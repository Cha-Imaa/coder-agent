import pytest

from bounded_queue import BoundedQueue


def test_fifo_order():
    q = BoundedQueue(3)
    q.push(1)
    q.push(2)
    q.push(3)
    assert [q.pop(), q.pop(), q.pop()] == [1, 2, 3]


def test_len_and_is_full_at_capacity():
    q = BoundedQueue(2)
    assert len(q) == 0 and not q.is_full()
    q.push("a")
    assert len(q) == 1 and not q.is_full()
    q.push("b")
    assert len(q) == 2 and q.is_full()


def test_push_to_full_raises_and_keeps_size():
    q = BoundedQueue(1)
    q.push("a")
    with pytest.raises(OverflowError):
        q.push("b")
    assert len(q) == 1


def test_pop_empty_raises():
    with pytest.raises(IndexError):
        BoundedQueue(1).pop()


@pytest.mark.parametrize("capacity", [0, -3])
def test_non_positive_capacity_raises(capacity):
    with pytest.raises(ValueError):
        BoundedQueue(capacity)
