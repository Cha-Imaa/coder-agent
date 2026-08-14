import doctest

import make_palindrome


def test_docstring_examples():
    result = doctest.testmod(make_palindrome, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
