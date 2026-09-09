import doctest

import mean_absolute_deviation


def test_docstring_examples():
    result = doctest.testmod(mean_absolute_deviation, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
