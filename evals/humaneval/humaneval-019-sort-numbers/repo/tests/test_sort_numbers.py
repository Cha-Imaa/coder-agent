import doctest

import sort_numbers


def test_docstring_examples():
    result = doctest.testmod(sort_numbers, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
