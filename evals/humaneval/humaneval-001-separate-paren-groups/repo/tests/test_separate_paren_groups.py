import doctest

import separate_paren_groups


def test_docstring_examples():
    result = doctest.testmod(separate_paren_groups, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
