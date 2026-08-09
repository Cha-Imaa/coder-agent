"""Smoke test: stream one reply from the configured model.

Run:  uv run python scripts/smoke_llm.py
Then open LangSmith to see the trace.
"""

from coder_agent.llm import get_llm


def main() -> None:
    llm = get_llm()
    print("Streaming reply from configured model...\n")
    for chunk in llm.stream("In two sentences, what is an AI agent?"):
        print(chunk.content, end="", flush=True)
    print("\n\nDone.")


if __name__ == "__main__":
    main()
