import doctest

import longest


def test_docstring_examples():
    result = doctest.testmod(longest, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
