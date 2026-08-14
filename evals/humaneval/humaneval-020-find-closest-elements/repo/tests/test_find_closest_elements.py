import doctest

import find_closest_elements


def test_docstring_examples():
    result = doctest.testmod(find_closest_elements, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
