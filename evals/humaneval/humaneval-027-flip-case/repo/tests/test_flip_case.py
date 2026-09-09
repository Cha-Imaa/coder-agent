import doctest

import flip_case


def test_docstring_examples():
    result = doctest.testmod(flip_case, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
