import doctest

import string_sequence


def test_docstring_examples():
    result = doctest.testmod(string_sequence, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
