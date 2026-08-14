import doctest

import sum_product


def test_docstring_examples():
    result = doctest.testmod(sum_product, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
