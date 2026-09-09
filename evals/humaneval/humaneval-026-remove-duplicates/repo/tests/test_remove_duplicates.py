import doctest

import remove_duplicates


def test_docstring_examples():
    result = doctest.testmod(remove_duplicates, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
