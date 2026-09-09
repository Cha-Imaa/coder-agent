import doctest

import greatest_common_divisor


def test_docstring_examples():
    result = doctest.testmod(greatest_common_divisor, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
