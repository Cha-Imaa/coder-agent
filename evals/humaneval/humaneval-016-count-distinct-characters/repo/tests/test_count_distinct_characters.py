import doctest

import count_distinct_characters


def test_docstring_examples():
    result = doctest.testmod(count_distinct_characters, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
