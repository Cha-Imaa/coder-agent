from textutils import truncate, word_count


def test_truncate_short_text_unchanged():
    assert truncate("hi", 5) == "hi"


def test_truncate_adds_ellipsis():
    assert truncate("hello world", 6) == "hello..."


def test_word_count():
    assert word_count("a  b c") == 3
