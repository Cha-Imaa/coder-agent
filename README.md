# coder-agent

[![ci](https://github.com/Cha-Imaa/coder-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Cha-Imaa/coder-agent/actions/workflows/ci.yml)
[![coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/Cha-Imaa/coder-agent/badges/coverage.json)](https://github.com/Cha-Imaa/coder-agent/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
[![docs](https://github.com/Cha-Imaa/coder-agent/actions/workflows/docs.yml/badge.svg)](https://cha-imaa.github.io/coder-agent/)

A terminal coding agent that takes a task in plain English, reads a target repository, plans a
change, edits files, runs the tests, and iterates on failures until they pass. Point it at an
issue URL and it opens the pull request.

Built to learn the agentic-AI stack end to end, on a zero-cost setup: every model call goes to a
free tier, every embedding is computed locally, and every file or shell action goes through a
sandbox jail.

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

The [docs site](https://cha-imaa.github.io/coder-agent/) has the learning log (one entry per
step: what was built, why, how production agents do it), the system design and the results.

## Quick start

```bash
git clone https://github.com/Cha-Imaa/coder-agent.git
cd coder-agent
uv sync --extra dev
cp .env.example .env   # fill in GROQ_API_KEY and GOOGLE_API_KEY (both free tiers)
```

```bash
uv run coder run path/to/repo "make the failing tests pass"          # one task, approve each edit
uv run coder chat path/to/repo                                        # several tasks on one thread
uv run coder fix-issue https://github.com/OWNER/REPO/issues/N --pr    # issue in, pull request out
uv run coder index path/to/repo --query "where are durations parsed"  # build and query the index
uv run coder stats                                                    # tokens and fallbacks per run
```

Flags worth knowing on `coder run`:

| Flag | What it does |
|---|---|
| `--yes` / `-y` | Skip the approval prompt before file edits and shell commands |
| `--sandbox docker` | Run the agent's commands in a fresh container, no network, repo mounted at `/work` |
| `--resume THREAD` | Continue a run that was interrupted by a crash, a rate limit or a declined approval |
| `--model provider:model` | Override `CODER_MODEL` for one run, e.g. `google_genai:gemini-2.5-flash` |
| `--max-iterations N` | Plan/act/test cycles before the agent gives up (default 4) |
| `--test-command "..."` | The command that decides success; auto-detected (pytest, npm test, ...) if omitted |

`fix-issue` clones the repository (or works in `--repo` your clone), runs the agent on a
`coder/issue-N` branch, and with `--pr` commits, pushes and opens the pull request once the tests
pass. Reading a public issue needs no token; `--pr` needs `GITHUB_TOKEN`.

## How it works

One run is one pass through a LangGraph state graph. The model never touches the filesystem
itself: every read, edit and command is a tool call served by an MCP server, and every tool call
that writes or executes stops at the `approve` node first unless `--yes` was given.

```mermaid
flowchart LR
    S([start]) --> prepare --> retrieve_context --> plan --> act
    act -- "tool calls" --> approve{"risky?"}
    approve -- "edit / write / run" --> H["human: approve or reject"]
    H -- yes --> tools
    H -- no --> act
    approve -- "read only" --> tools
    tools --> act
    act -- "model stopped" --> run_tests
    run_tests -- "red, iterations left" --> reflect --> plan
    run_tests -- "green, or out of iterations" --> finish
    act -- "step budget spent" --> finish
    finish --> E([end])
```

| Node | What it does |
|---|---|
| `prepare` | Detects the test command (pytest, npm test, ...) unless the caller supplied one |
| `retrieve_context` | Queries the local index with the task, puts the top chunks into the state as context (inert when retrieval is off) |
| `plan` | One model call: a short numbered plan from task, context and, after a red run, the test output |
| `act` | The tool-calling loop: the model reads, searches, edits and runs commands until it says it is done |
| `approve` | `interrupt()`s the run before `edit_file`, `write_file` or `run_command`; the terminal shows the diff or command and asks |
| `tools` | LangGraph's `ToolNode` executing the calls against the MCP servers, errors returned as tool output |
| `run_tests` | Runs the test command in the sandbox and records pass/fail plus the output |
| `reflect` | No model call: appends the failing test output as a new turn so the next plan starts from evidence |
| `finish` | Derives the final status (passed, failed, gave up) and summary; the caller then writes tokens per node, latency and outcome to the SQLite ledger |

Around the graph:

```mermaid
flowchart TB
    CLI["coder CLI<br/>run · chat · fix-issue · index · stats"]
    subgraph Graph["LangGraph (SQLite checkpointer)"]
        G["prepare → retrieve → plan → act ⇄ tools → run_tests → reflect"]
    end
    subgraph MCP["MCP servers (stdio)"]
        FS["files + shell<br/>read_file · edit_file · write_file<br/>list_dir · search_code · run_command"]
        GH["GitHub<br/>get_issue · open_pull_request"]
    end
    subgraph Sandbox["Sandbox"]
        L["local: path jail, denylist, timeout"]
        D["docker: fresh container, no network"]
    end
    subgraph RAG["Retrieval (local, CPU)"]
        R["tree-sitter chunks → fastembed + Chroma<br/>BM25 + dense, RRF · optional reranker"]
    end
    LLM["Groq gpt-oss-120b<br/>retry → Gemini 2.5 Flash fallback"]
    CLI --> Graph
    Graph --> LLM
    Graph --> MCP
    FS --> Sandbox
    Graph --> RAG
    Graph --> Ledger["telemetry ledger (SQLite)<br/>+ LangSmith traces"]
```

The MCP servers are separate processes talking JSON-RPC over stdio. The file server resolves
every path inside the repository and hands `run_command` to the sandbox, which is either the
local jail (denylist, timeout) or a fresh Docker container with `--sandbox docker`; the graph
sees the same tools either way. The GitHub server is the same shape as any third-party MCP
server the agent could be pointed at. The concept walkthrough is in
[`docs/system_design_coding_agent.md`](docs/system_design_coding_agent.md).

## Results

Every number comes from a script in the repository and a results file under `evals/results/`;
`evals/figures.py` redraws the charts from those files. The full write-up, including the
retrieval-mode and reranker tables, is on the
[results page](https://cha-imaa.github.io/coder-agent/results/).

### In-house suite

Twelve tasks with hidden tests across five categories, run with `groq:openai/gpt-oss-120b`, up
to four plan/act/test iterations, retrieval on. Every task was solved in one cycle. Six tasks
first errored on the free-tier quota (Groq allows 200k tokens per rolling 24 hours) and were
rerun with `--rerun-errors`; the results file records both commits.

| Category | Tasks | Passed | Errors | pass@1 | Avg tokens | Avg iterations |
|---|---|---|---|---|---|---|
| fix-bug | 3 | 3 | 0 | 100% | 12,443 | 1.0 |
| add-feature | 3 | 3 | 0 | 100% | 28,984 | 1.0 |
| refactor | 2 | 2 | 0 | 100% | 25,451 | 1.0 |
| add-test | 2 | 2 | 0 | 100% | 19,222 | 1.0 |
| multi-file | 2 | 2 | 0 | 100% | 71,164 | 1.0 |
| total | 12 | 12 | 0 | 100% | 29,663 | 1.0 |

![pass@1 by task category](docs/figures/pass_rate.png)

The suite is small and the tasks are single-purpose, so 100% says the loop works, not that the
agent is done. A `--agent noop` run that touches nothing fails all twelve, so there are no free
passes in the suite.

![Share of tasks solved after each iteration](docs/figures/iteration_curve.png)

### Where the tokens go

About 97% of the tokens per run are spent in the `act` node reading files and running tools,
around 29k against under 1k for `plan`. That is the number codebase retrieval is meant to move.

![Tokens per run by graph node](docs/figures/cost_profile.png)

### Retrieval quality

`evals/retrieval_eval.py` uses each task prompt as the query and scores the files in hit order
against the gold files of the task, over 40 tasks (ten in-house, thirty HumanEval).

| mode | recall@1 | recall@3 | recall@5 | MRR |
|---|---|---|---|---|
| hybrid | 0.08 | 0.99 | 1.00 | 0.56 |
| hybrid + rerank | 0.50 | 0.99 | 1.00 | 0.76 |

The benchmark repositories have three to five files, so file-level recall saturates at k=3 and
the first-stage modes (bm25, dense, hybrid) are indistinguishable on this corpus. Without
reranking the first hit is almost always the visible test file; the cross-encoder moves the
source module to rank one in half of the cases. It stays off by default: about 1.5 seconds per
query for a change in ordering, not in which chunks are present.

### Still being measured

- Retrieval ablation on the suite (off, BM25, dense, hybrid): the `off` arm is five of twelve
  tasks in; the rest waits on the daily quota, and the figure draws itself once two arms are
  complete.
- HumanEval slice (thirty problems packaged as repository tasks) and a model comparison with a
  local Ollama model.

### Reproduce

```bash
uv run python evals/run_evals.py                          # 12-task suite, JSON to evals/results/
uv run python evals/run_evals.py --suite humaneval        # 30 HumanEval problems as repo tasks
uv run python evals/run_evals.py --retrieval off          # one ablation arm
uv run python evals/retrieval_eval.py                     # recall@k per retrieval mode
uv run python evals/figures.py                            # redraw docs/figures/*.png
uv run pytest                                             # the test suite, no API key needed
```

## Project layout

```
src/coder_agent/
  cli.py            Typer commands: run, chat, fix-issue, index, stats
  agent.py          builds the graph, MCP clients and checkpointer for one session
  graph/            state, nodes, routing, approval interrupt, context trimming
  mcp_server/       server.py (files + shell, jailed) and github.py (issues, PRs)
  sandbox/          local jail and Docker runner behind one interface
  rag/              loader, tree-sitter chunker, embeddings, Chroma index, hybrid retriever, reranker
  llm.py            provider factory, retry with backoff, fallback chain
  telemetry/        SQLite ledger of tokens, latency, iterations, outcome per run
  evals/            task suites, isolated runner, retrieval eval, figures
evals/              the 12-task suite, the HumanEval slice, results and entry-point scripts
docs/               learning log (TEACH.md), roadmap (PLAN.md), system design, results, figures
tests/              pytest, no network and no API key needed
```

## Learning log and roadmap

- [`docs/TEACH.md`](docs/TEACH.md): one entry per finished step, written for someone learning the
  stack. Each entry says what was built, why that way, how production coding agents do the same
  thing, and gives a command to check it.
- [`docs/PLAN.md`](docs/PLAN.md): the weighted roadmap; checked boxes are finished steps.
- [`docs/system_design_coding_agent.md`](docs/system_design_coding_agent.md): the concept-first
  walkthrough of agents, LangGraph, MCP, sandboxing, retrieval and evaluation.
