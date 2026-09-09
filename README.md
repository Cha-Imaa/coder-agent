# coder-agent

A terminal coding agent that takes a task in plain English, reads a target repository, plans a change,
edits files, runs the tests, and iterates on failures until they pass.

Built to learn the agentic-AI stack end to end, on a zero-cost setup:

| Concern | Technology |
|---|---|
| Agent orchestration | LangGraph state graph (plan → act → test → reflect) |
| Tools | Model Context Protocol (MCP) server exposing file and shell tools |
| Codebase retrieval | Local embeddings (bge-small) + Chroma vector store |
| LLM | Groq free tier, Gemini free tier as fallback, swappable via one env var |
| Observability | LangSmith tracing |
| Interface | Typer + Rich CLI |

## Status

Work in progress. See the commit history for the step-by-step build.

### First numbers

In-house suite of 12 tasks with hidden tests, `groq:openai/gpt-oss-120b`, up to 4 iterations.
Every task the model ran was solved in one iteration; the six errors are free-tier daily quota
exhaustion (Groq 200k tokens/day, Gemini fallback 20 requests/day), not agent failures.

| Category | Tasks | Passed | Errors | pass@1 | Avg tokens |
|---|---|---|---|---|---|
| fix-bug | 3 | 1 | 2 | 33% | 3,289 |
| add-feature | 3 | 3 | 0 | 100% | 28,984 |
| refactor | 2 | 0 | 2 | 0% | 0 |
| add-test | 2 | 2 | 0 | 100% | 19,222 |
| multi-file | 2 | 0 | 2 | 0% | 0 |
| total | 12 | 6 | 6 | 50% | 11,272 |

Reproduce with `uv run python evals/run_evals.py`; raw results are in `evals/results/`.

A second suite packages the first 30 [HumanEval](https://github.com/openai/human-eval) problems
as repository tasks (stub module, docstring examples as the visible doctest, the original
`check` as the hidden test): `uv run python evals/run_evals.py --suite humaneval`. Numbers for it
follow once the daily quota allows a full run.

## Setup

```bash
uv sync --extra dev
cp .env.example .env   # fill in your keys
```
