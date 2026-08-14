import doctest

import filter_integers


def test_docstring_examples():
    result = doctest.testmod(filter_integers, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
