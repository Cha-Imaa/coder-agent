"""A FIFO queue with a fixed maximum size."""


class BoundedQueue:
    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._items: list = []

    def push(self, item) -> None:
        """Append at the back; OverflowError when the queue is full."""
        if self.is_full():
            raise OverflowError("queue is full")
        self._items.append(item)

    def pop(self):
        """Remove and return the front item; IndexError when empty."""
        if not self._items:
            raise IndexError("pop from empty queue")
        return self._items.pop(0)

    def is_full(self) -> bool:
        return len(self._items) >= self.capacity

    def __len__(self) -> int:
        return len(self._items)
