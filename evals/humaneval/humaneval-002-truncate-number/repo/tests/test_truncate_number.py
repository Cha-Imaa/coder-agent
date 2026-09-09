import doctest

import truncate_number


def test_docstring_examples():
    result = doctest.testmod(truncate_number, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
