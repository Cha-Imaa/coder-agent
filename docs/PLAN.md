# Build plan

A terminal coding agent that takes a task in plain English, reads a repository, plans a change, edits
files, runs the tests, and iterates until they pass. Everything runs on free tiers or locally.

## Goals

1. Learn the agentic-AI stack by building each layer by hand: LangGraph orchestration, MCP tools,
   retrieval over a codebase, observability, and evaluation.
2. Ship something usable: `coder run <repo> "<task>"` should fix a failing test in a small repo
   without human edits.
3. Zero cost: Groq and Gemini free tiers for the LLM, CPU embeddings, local Chroma, LangSmith free tier.

## Architecture

```
START -> retrieve_context -> plan -> act <-> tools -> run_tests -> reflect
                                     ^                              |
                                     +------- failed & iter < N ----+
                                                                    +-- passed | iter >= N --> finish -> END
```

| Layer | Module | Responsibility |
|---|---|---|
| CLI | `cli.py` | `coder run`, `coder index`, `coder chat`; streams graph events with Rich |
| Config | `config.py` | Typed settings from `.env` (model, iterations, timeouts) |
| LLM | `llm.py` | `init_chat_model` + fallback provider on rate limit |
| Graph | `graph/` | State schema, nodes, edges, checkpointer |
| Tools | `mcp_server/`, `tools/` | MCP server exposing file/shell tools; adapter that loads them as LangChain tools |
| Sandbox | `sandbox/` | Path jail, command denylist, timeouts; optional Docker runner |
| Retrieval | `rag/` | Language-aware chunking, incremental Chroma index, hybrid retriever |
| Evals | `evals/` | Toy repos with failing tests; pass-rate and token reports |

### Safety rails
- Every path is resolved and must stay inside the target repo.
- Shell commands run with a timeout, capped output, and a denylist (`rm -rf`, `git push`, package installs unless allowed).
- File writes show a diff and require confirmation unless `--yes`.

## Milestones

Each milestone ends in a working, pushed state. Every step carries a weight in points `(Npt)`,
a rough estimate of effort relative to the other steps. Progress is checked points over total
points, not a step count, so finishing a big step moves the bar more than a small one.

### 0. Scaffold
- [x] Project layout, `pyproject.toml`, `uv` environment, `.env.example` (2pt)
- [x] Typed settings and LLM factory with fallback (3pt)
- [ ] Smoke test streams a reply; first trace visible in LangSmith (1pt)

### 1. MCP tool server
- [x] FastMCP server: `read_file`, `edit_file`, `write_file`, `list_dir`, `search_code`, `run_command` (4pt)
- [x] Path jail and command denylist with unit tests (3pt)
- [x] Client loads the tools via `langchain-mcp-adapters`; end-to-end tests over stdio (3pt)
- [ ] Inspect the server with MCP Inspector (1pt)

### 2. Minimal agent loop
- [ ] Graph state, `plan` and `act` nodes, `ToolNode`, `finish` (5pt)
- [ ] `coder run` streams node transitions and tool calls (3pt)
- [ ] Agent adds a function to a toy repo end to end (3pt)

### 3. Test-and-fix loop
- [ ] `run_tests` node with test-command detection (3pt)
- [ ] `reflect` node and conditional edge with `max_iterations` (3pt)
- [ ] Three eval tasks and `run_evals.py` reporting pass rate (4pt)

### 4. Retrieval over the codebase
- [ ] Repo loader honouring `.gitignore`, language-aware chunking (4pt)
- [ ] Incremental Chroma index keyed by file hash; `coder index` (4pt)
- [ ] `retrieve_context` node; measure pass rate and tokens with and without retrieval (3pt)

### 5. Hardening and polish
- [ ] Provider fallback verified under real rate limits; Docker sandbox mode (4pt)
- [ ] `coder chat` with SQLite checkpointer and thread resume (3pt)
- [ ] README: architecture, demo, eval table, lessons learned (2pt)

### Stretch
- Human-in-the-loop `interrupt()` before writes
- Second MCP server (GitHub or web search) to show multi-server composition
- Reranker or pgvector; local model via Ollama for comparison

## Verification

- `pytest tests/`: chunker, jail, denylist, graph routing
- MCP Inspector lists five tools and `read_file` round-trips
- `coder run evals/tasks/01_fix_bug "make the tests pass"` succeeds with a full LangSmith trace
- `python evals/run_evals.py` reports at least 5/8 passing on the toy suite
