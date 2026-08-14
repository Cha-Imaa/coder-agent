"""Tiny command dispatcher; returns the text a real CLI would print."""

from notes.service import NoteService


def main(argv: list[str], service: NoteService) -> str:
    if not argv:
        return "usage: add <text> | show <id> | list"
    command, *args = argv
    if command == "add":
        note_id = service.create(" ".join(args))
        return f"added {note_id}"
    if command == "show":
        text = service.read(int(args[0]))
        return text if text is not None else f"no such note {args[0]}"
    if command == "list":
        return " ".join(str(i) for i in service.list_ids()) or "(empty)"
    return f"unknown command: {command}"
