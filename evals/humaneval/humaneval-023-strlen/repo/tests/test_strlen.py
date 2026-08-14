import doctest

import strlen


def test_docstring_examples():
    result = doctest.testmod(strlen, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
