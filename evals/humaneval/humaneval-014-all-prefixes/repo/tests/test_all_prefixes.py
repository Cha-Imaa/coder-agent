import doctest

import all_prefixes


def test_docstring_examples():
    result = doctest.testmod(all_prefixes, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
