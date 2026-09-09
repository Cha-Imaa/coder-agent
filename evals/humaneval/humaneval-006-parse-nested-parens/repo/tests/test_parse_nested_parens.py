import doctest

import parse_nested_parens


def test_docstring_examples():
    result = doctest.testmod(parse_nested_parens, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
