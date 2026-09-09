import doctest

import largest_divisor


def test_docstring_examples():
    result = doctest.testmod(largest_divisor, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
