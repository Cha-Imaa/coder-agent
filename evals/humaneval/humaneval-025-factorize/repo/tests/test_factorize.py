import doctest

import factorize


def test_docstring_examples():
    result = doctest.testmod(factorize, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
