import doctest

import below_zero


def test_docstring_examples():
    result = doctest.testmod(below_zero, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
