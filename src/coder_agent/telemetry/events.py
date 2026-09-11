"""Count what went wrong on the way to the model, per run.

LangChain emits callback events for every model call; `on_llm_error` fires once per failed
attempt, whether the retry layer or the fallback layer handles it afterwards. Passing one
`ProviderEvents` in the run config (`{"callbacks": [events]}`) makes it flow down to every
node's model call, so at the end of the run `events.errors` is the count of provider failures
by exception type, e.g. `{"RateLimitError": 3, "BadRequestError": 1}`. The ledger stores it
next to which model finally answered, which is how fallback behaviour gets measured instead of
guessed.
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import BaseCallbackHandler


class ProviderEvents(BaseCallbackHandler):
    def __init__(self) -> None:
        super().__init__()
        self.errors: dict[str, int] = {}

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        name = type(error).__name__
        self.errors[name] = self.errors.get(name, 0) + 1

    @property
    def total(self) -> int:
        return sum(self.errors.values())
