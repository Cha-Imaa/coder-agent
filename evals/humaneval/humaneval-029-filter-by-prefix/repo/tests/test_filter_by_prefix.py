import doctest

import filter_by_prefix


def test_docstring_examples():
    result = doctest.testmod(filter_by_prefix, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
