import doctest

import concatenate


def test_docstring_examples():
    result = doctest.testmod(concatenate, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
