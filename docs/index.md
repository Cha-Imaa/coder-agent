# coder-agent

A terminal coding agent that takes a task in plain English, reads a target repository, plans a
change, edits files, runs the tests, and iterates on failures until they pass.

It is built to learn the agentic-AI stack end to end, on a zero-cost setup: every model call goes
to a free tier, every embedding is computed locally, and every filesystem or shell action goes
through a sandbox jail.

| Concern | Technology |
|---|---|
| Agent orchestration | LangGraph state graph: plan, act, test, reflect |
| Tools | Two Model Context Protocol (MCP) servers: file and shell tools scoped to the repo, GitHub issues and pull requests |
| Codebase retrieval | tree-sitter chunking, local embeddings and Chroma, BM25 + dense with reciprocal rank fusion, optional cross-encoder reranker |
| Memory and control | SQLite checkpointer, `--resume`, human approval before writes and risky commands |
| Sandbox | Path jail, command denylist, timeouts, or a throwaway Docker container |
| LLM | Groq free tier, Gemini free tier as fallback, retry with backoff, swappable via one env var |
| Observability | LangSmith tracing and a local SQLite ledger of tokens per node |
| Interface | Typer + Rich CLI: `coder run`, `coder chat`, `coder fix-issue`, `coder index`, `coder stats` |

## How to read this site

- **[Results](results.md)** is the numbers page: pass rate on the in-house suite, where the tokens
  go, retrieval quality with and without the reranker, and what is still being measured.
- **[Learning log](TEACH.md)** has one entry per finished step: what was built, why that way, how
  production coding agents do the same thing, and a command that checks it. Read it top to
  bottom to be able to explain every file in `src/`.
- **[System design](system_design_coding_agent.md)** is the concept-first walkthrough: what an
  agent is, why LangGraph rather than a `while` loop, tools as a protocol, sandboxing, retrieval,
  evaluation, and how the big coding agents are put together.
- **[Roadmap](PLAN.md)** is the weighted build plan; checked boxes are finished steps.

## Quick start

```bash
git clone https://github.com/Cha-Imaa/coder-agent.git
cd coder-agent
uv sync --extra dev
cp .env.example .env   # fill in GROQ_API_KEY and GOOGLE_API_KEY
```

```bash
uv run coder run path/to/repo "make the failing tests pass"          # one task, approve each edit
uv run coder chat path/to/repo                                        # several tasks on one thread
uv run coder fix-issue https://github.com/OWNER/REPO/issues/N --pr    # issue in, pull request out
uv run coder index path/to/repo --query "where are durations parsed"  # build and query the index
uv run coder stats                                                    # tokens and fallbacks per run
```

`fix-issue` clones the repository (or works in `--repo` your clone), runs the agent on a
`coder/issue-N` branch, and with `--pr` commits, pushes and opens the pull request once the tests
pass. Reads need no token; `--pr` needs `GITHUB_TOKEN`.

## Reproduce the numbers

```bash
uv run python evals/run_evals.py                 # 12-task in-house suite, results to evals/results/
uv run python evals/run_evals.py --suite humaneval
uv run python evals/retrieval_eval.py            # recall@k of the gold files per retrieval mode
uv run python evals/figures.py                   # redraw docs/figures/*.png from the results
uv run pytest                                    # the test suite, no API key needed
```

The source is on [GitHub](https://github.com/Cha-Imaa/coder-agent); the commit history is the
step-by-step build.
