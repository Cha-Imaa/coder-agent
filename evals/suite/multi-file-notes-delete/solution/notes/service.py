"""Application logic over Storage; the CLI never touches storage directly."""

from notes.storage import Storage


class NoteService:
    def __init__(self, storage: Storage | None = None) -> None:
        self.storage = storage or Storage()

    def create(self, text: str) -> int:
        text = text.strip()
        if not text:
            raise ValueError("note text is empty")
        return self.storage.put(text)

    def read(self, note_id: int) -> str | None:
        try:
            return self.storage.get(note_id)
        except KeyError:
            return None

    def delete(self, note_id: int) -> bool:
        try:
            self.storage.delete(note_id)
        except KeyError:
            return False
        return True

    def list_ids(self) -> list[int]:
        return self.storage.ids()
