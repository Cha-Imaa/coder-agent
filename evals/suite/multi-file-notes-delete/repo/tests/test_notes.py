from notes.cli import main
from notes.service import NoteService


def test_add_show_list():
    svc = NoteService()
    assert main(["add", "buy", "milk"], svc) == "added 1"
    assert main(["show", "1"], svc) == "buy milk"
    assert main(["list"], svc) == "1"


def test_show_missing():
    assert main(["show", "9"], NoteService()) == "no such note 9"
