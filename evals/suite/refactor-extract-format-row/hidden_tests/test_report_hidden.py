from report import format_row, render_report, render_summary

ROWS = [("apple", 3, 1.5), ("pear", 0, 2.0), ("melon", 1, 4.25)]


def test_format_row_matches_report_line():
    assert format_row("apple", 3, 1.5) == "apple           3      1.50"
    assert format_row("melon", 1, 4.25) == render_report(ROWS).splitlines()[2]


def test_report_output_unchanged():
    assert render_report(ROWS) == (
        "apple           3      1.50\n"
        "pear            0      2.00\n"
        "melon           1      4.25\n"
        "TOTAL                  8.75"
    )


def test_summary_output_unchanged():
    assert render_summary(ROWS) == (
        "IN STOCK\n"
        "apple           3      1.50\n"
        "melon           1      4.25"
    )
