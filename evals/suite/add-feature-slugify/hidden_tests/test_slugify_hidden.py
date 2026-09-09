import pytest

from textutils import slugify


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Hello, World!", "hello-world"),
        ("  Many   spaces  here ", "many-spaces-here"),
        ("already-a-slug", "already-a-slug"),
        ("Under_score & Ampersand", "under-score-ampersand"),
        ("Version 2.0.1", "version-2-0-1"),
        ("", ""),
        ("!!!", ""),
    ],
)
def test_slugify(text, expected):
    assert slugify(text) == expected
