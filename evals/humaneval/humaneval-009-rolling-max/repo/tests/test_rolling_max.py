import doctest

import rolling_max


def test_docstring_examples():
    result = doctest.testmod(rolling_max, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
