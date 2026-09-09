from report import render_report, render_summary

ROWS = [("apple", 3, 1.5), ("pear", 0, 2.0), ("melon", 1, 4.25)]


def test_render_report_golden():
    assert render_report(ROWS) == (
        "apple           3      1.50\n"
        "pear            0      2.00\n"
        "melon           1      4.25\n"
        "TOTAL                  8.75"
    )


def test_render_summary_skips_zero_qty():
    assert render_summary(ROWS) == (
        "IN STOCK\n"
        "apple           3      1.50\n"
        "melon           1      4.25"
    )
