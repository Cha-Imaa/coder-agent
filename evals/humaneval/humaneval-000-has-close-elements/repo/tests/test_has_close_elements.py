import doctest

import has_close_elements


def test_docstring_examples():
    result = doctest.testmod(has_close_elements, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
