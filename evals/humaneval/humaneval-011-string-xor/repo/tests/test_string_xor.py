import doctest

import string_xor


def test_docstring_examples():
    result = doctest.testmod(string_xor, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
