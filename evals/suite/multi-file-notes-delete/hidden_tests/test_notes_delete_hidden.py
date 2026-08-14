import pytest

from notes.cli import main
from notes.service import NoteService
from notes.storage import Storage


def test_storage_delete_removes_and_raises_on_unknown():
    s = Storage()
    nid = s.put("x")
    s.delete(nid)
    assert s.ids() == []
    with pytest.raises(KeyError):
        s.delete(nid)


def test_service_delete_returns_bool():
    svc = NoteService()
    nid = svc.create("x")
    assert svc.delete(nid) is True
    assert svc.delete(nid) is False
    assert svc.read(nid) is None


def test_cli_delete_messages():
    svc = NoteService()
    main(["add", "hello"], svc)
    assert main(["delete", "1"], svc) == "deleted 1"
    assert main(["delete", "1"], svc) == "no such note 1"
    assert main(["list"], svc) == "(empty)"
