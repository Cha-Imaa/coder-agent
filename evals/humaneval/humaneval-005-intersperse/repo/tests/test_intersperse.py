import doctest

import intersperse


def test_docstring_examples():
    result = doctest.testmod(intersperse, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
