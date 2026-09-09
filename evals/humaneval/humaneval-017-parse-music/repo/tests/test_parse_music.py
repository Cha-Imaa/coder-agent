import doctest

import parse_music


def test_docstring_examples():
    result = doctest.testmod(parse_music, verbose=False)
    assert result.attempted > 0
    assert result.failed == 0
