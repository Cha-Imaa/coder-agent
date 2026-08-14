"""In-memory note storage keyed by integer id."""


class Storage:
    def __init__(self) -> None:
        self._notes: dict[int, str] = {}
        self._next_id = 1

    def put(self, text: str) -> int:
        note_id = self._next_id
        self._next_id += 1
        self._notes[note_id] = text
        return note_id

    def get(self, note_id: int) -> str:
        return self._notes[note_id]

    def ids(self) -> list[int]:
        return sorted(self._notes)
