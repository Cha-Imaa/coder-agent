import pytest

from slicer import chunk


def test_partial_tail():
    assert chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]


def test_size_larger_than_input():
    assert chunk([1, 2], 5) == [[1, 2]]


def test_empty_input():
    assert chunk([], 3) == []


def test_size_one():
    assert chunk(["a", "b", "c"], 1) == [["a"], ["b"], ["c"]]


def test_rejects_non_positive_size():
    with pytest.raises(ValueError):
        chunk([1], 0)
