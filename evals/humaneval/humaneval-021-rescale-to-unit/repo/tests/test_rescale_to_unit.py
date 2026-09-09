import doctest

import rescale_to_unit


def test_docstring_examples():
    result = doctest.testmod(rescale_to_unit, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
