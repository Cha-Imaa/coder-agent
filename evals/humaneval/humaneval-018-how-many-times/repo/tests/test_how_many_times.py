import doctest

import how_many_times


def test_docstring_examples():
    result = doctest.testmod(how_many_times, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
