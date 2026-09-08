# System design: a coding agent from first principles

This document explains what we are building, why each piece exists, and how the pieces fit. It is
written to teach: each section introduces one concept, then shows how this project uses it.

---

## 1. What is an agent?

A plain LLM call is a function: text in, text out. It cannot look at your files, run your tests, or
try again when it is wrong.

An **agent** is an LLM placed inside a loop with tools:

```
while not done:
    response = llm(messages)                 # the model decides what to do next
    if response.has_tool_calls:
        results = run(response.tool_calls)   # we execute them
        messages += results                  # and feed the results back
    else:
        done = True
```

Three things make this an agent rather than a chatbot:

1. **Tools**: functions the model may call (read a file, run a command). The model does not run them;
   it emits a structured request and our code runs it.
2. **State**: the growing conversation plus anything else we track (the plan, test results, iteration
   count).
3. **Control flow**: the loop, plus rules for when to stop, when to retry, and when to ask a human.

A coding agent is this loop specialised for software: its tools touch a repository and its stop
condition is "the tests pass".

### Tool calling, concretely

Modern chat models support **native tool calling**. We send a JSON schema for each tool; the model
returns a message like:

```json
{"tool_calls": [{"name": "read_file", "args": {"path": "src/app.py"}}]}
```

We execute it and append a `ToolMessage` containing the output. The model never sees our Python
functions, only their names, descriptions and argument schemas. Writing good descriptions is
therefore part of prompt engineering.

---

## 2. Why LangGraph instead of a `while` loop

The loop above works for demos. Real agents need:

- **Branching**: after tests run, go to `reflect` on failure or `finish` on success.
- **Bounded retries**: stop after N iterations so a confused model cannot burn the whole budget.
- **Persistence**: pause for human approval, resume later, inspect state after each step.
- **Observability**: know which step produced which output.

**LangGraph** models the agent as a **state graph**:

- **State** is a typed dictionary (a `TypedDict`) that every node reads and partially updates.
- **Nodes** are plain Python functions `state -> partial_state`.
- **Edges** connect nodes. **Conditional edges** are functions that look at state and return the
  name of the next node.
- A **checkpointer** saves state after every node, which gives resume, time-travel and
  human-in-the-loop for free.

Our graph:

```
START -> retrieve_context -> plan -> act <-> tools -> run_tests -> reflect
                                     ^                              |
                                     +------- failed & iter < N ----+
                                                                    +-- passed | iter >= N --> finish -> END
```

| Node | Reads | Writes | Uses LLM? |
|---|---|---|---|
| `retrieve_context` | task | relevant code chunks, file tree | no (embeddings only) |
| `plan` | task, context | numbered plan, likely files | yes, structured output |
| `act` | plan, messages | tool calls (edits, reads) | yes, tool calling |
| `tools` | tool calls | tool results | no |
| `run_tests` | repo | exit code, stdout | no |
| `reflect` | test output, plan | revised plan, iteration+1 | yes |
| `finish` | everything | summary for the user | yes |

**Key design idea**: the LLM only decides *inside* nodes. The *shape* of the loop (when to test,
when to stop) is deterministic Python. This makes the agent predictable and debuggable.

### Structured output

For `plan` we do not want free text; we want a list of steps and file paths we can render and
check. `llm.with_structured_output(PlanSchema)` forces the model to return JSON matching a Pydantic
model. Under the hood it uses the same tool-calling mechanism: the schema is presented as a tool
the model "calls" exactly once.

---

## 3. Tools as a protocol: MCP

We could define tools as Python functions inside the agent. Instead we expose them through the
**Model Context Protocol (MCP)**.

MCP is a client-server protocol (JSON-RPC over stdio or HTTP). A **server** advertises tools,
resources and prompts; a **client** (the agent, an IDE, a desktop assistant) discovers and calls
them. It decouples "who provides capabilities" from "who consumes them".

Why bother for a solo project:

- The same file/shell server can be plugged into any MCP-capable client, so it is reusable.
- You learn the protocol the industry is converging on for agent tooling.
- It forces a clean boundary: the agent talks JSON to a process; it cannot reach the filesystem
  except through declared tools.

How we use it:

- `mcp_server/server.py` uses **FastMCP** from the official Python SDK. A tool is a decorated
  function; the SDK derives the JSON schema from type hints and the docstring.
- The agent starts the server as a subprocess and speaks to it over **stdio** (stdin/stdout).
- `langchain-mcp-adapters` converts the discovered MCP tools into LangChain `BaseTool` objects so
  LangGraph's `ToolNode` can call them.

```
LangGraph ToolNode -> LangChain tool -> MCP client -> stdio -> MCP server -> sandbox -> filesystem
```

Tools we expose: `read_file`, `write_file`, `list_dir`, `search_code`, `run_command`.

---

## 4. Sandboxing: an agent that can edit files can also delete them

Every tool call goes through `sandbox/`:

- **Path jail**: resolve every path to an absolute path and assert it is inside the target repo.
  A path like `../../.ssh/id_rsa` must fail.
- **Command policy**: a denylist (`rm -rf`, `git push`, package installs unless allowed) plus a
  timeout and an output cap, so a runaway loop cannot hang the agent or flood the context window.
- **Confirmation gate**: the CLI shows a unified diff and asks before writing, unless `--yes`.
- **Docker mode (later)**: run tests inside a container with the repo mounted, so even an unsafe
  command cannot touch the host.

Principle: the model is untrusted input. Treat its tool calls the way a web server treats request
parameters.

---

## 5. Retrieval: giving the agent a map of the codebase (RAG)

Context windows are finite and tokens cost money (or, on free tiers, rate-limit budget). Dumping a
whole repository into the prompt does not scale. **Retrieval-Augmented Generation** fetches only
the relevant pieces.

Pipeline:

1. **Load**: walk the repo, skip ignored files and binaries.
2. **Chunk**: split files into pieces. We split **by language syntax** (functions, classes) using
   `RecursiveCharacterTextSplitter.from_language`, so a chunk is a meaningful unit, not an
   arbitrary 500 characters.
3. **Embed**: turn each chunk into a vector with a local model (`BAAI/bge-small-en-v1.5`, runs on
   CPU). Similar meaning gives nearby vectors.
4. **Store**: save vectors plus metadata (file path, line range) in **Chroma**, a local vector
   database.
5. **Retrieve**: embed the task, find the top-k nearest chunks, add a cheap keyword boost for
   exact identifiers, and put them in the prompt.

**Incremental indexing**: each chunk is keyed by a hash of its file, so re-indexing only touches
changed files.

We will measure whether retrieval helps: same eval tasks with and without the `retrieve_context`
node, comparing pass rate and tokens used. That measurement is the actual lesson.

---

## 6. Observability: LangSmith

An agent that fails silently is impossible to improve. **LangSmith** records every LLM call, tool
call, and graph node as a nested trace: inputs, outputs, latency, tokens.

Setting `LANGSMITH_TRACING=true` is enough; LangChain and LangGraph instrument themselves. We will
use traces to answer questions like "why did the model edit the wrong file" and "how many tokens
does one fix cost".

---

## 7. Evaluation: how do we know it works?

We build a tiny benchmark: `evals/tasks/` holds small repos, each with a failing test and a
`task.md` describing the job. `run_evals.py` runs the agent on each and reports:

- pass rate
- iterations used
- tokens and wall time

Every change to prompts or the graph is judged against this number, not against a feeling.

---

## 8. Model choice under a zero-cost constraint

| Option | Verdict |
|---|---|
| Local 7B model on this laptop (CPU, no GPU) | Too slow and too weak at multi-step tool calling |
| Groq free tier (Llama 3.3 70B class) | Fast, native tool calling, good quality. **Primary** |
| Gemini 2.5 Flash free tier | Good quality, separate rate-limit pool. **Fallback** |

LangChain's `init_chat_model("provider:model")` plus `with_fallbacks` lets us switch or chain
providers with no code change. Rate limits are the main cost of "free", so the fallback matters.

---

## 9. The CLI

`typer` turns functions into commands; `rich` renders diffs, tables and streaming output.

```
coder index <repo>                 # build/refresh the vector index
coder run   <repo> "<task>"        # one-shot: plan, edit, test, iterate
coder chat  <repo>                 # multi-turn session with resume (checkpointer)
```

While the graph runs, the CLI streams events: which node is active, which tool was called, the diff
about to be applied, the test output. You watch the agent think.

---

## 10. Build order and why

1. **Scaffold + LLM factory**: prove we can call a model and see a trace.
2. **MCP server + sandbox**: tools first, tested in isolation, before any LLM touches them.
3. **Minimal graph (plan, act, finish)**: the smallest agent that edits a file.
4. **Test-and-fix loop**: add `run_tests` and `reflect`; this is where it becomes an agent.
5. **Retrieval**: add RAG and measure the difference.
6. **Hardening**: fallback, Docker, chat mode, README.

Each step leaves a working, pushed state. Each step adds one concept.

---

## 11. How production coding agents are built, and what we mirror

The goal of this project is to understand tools like Claude Code, Codex CLI, Aider and Cursor's agent
mode from the inside. Publicly documented behaviour of those tools shows a shared anatomy. Here is
that anatomy, and where each piece lives in our build.

### 11.1 The core is a small loop, not a big framework

Every production coding agent is, at its centre, the loop from section 1: send messages plus tool
schemas to the model, execute the tool calls it returns, append results, repeat until the model
answers with no tool calls. Claude Code's loop is exactly this, running against the Messages API
with a fixed tool set. The sophistication is in everything around the loop, not the loop itself.

Ours: LangGraph's `act <-> tools` cycle. We make the loop explicit as graph edges so you can see
and instrument it.

### 11.2 A small, general tool set beats many specific tools

Claude Code ships roughly a dozen tools: read file, edit file (search and replace), write file,
run shell command, search file contents (grep), find files (glob), list directory, plus web fetch,
task delegation and a to-do list. There is no "refactor" tool or "fix bug" tool. General primitives
let the model compose any workflow; the intelligence stays in the model.

Two details worth copying:

- **Edit is search-and-replace, not whole-file rewrite.** The model supplies `old_string` and
  `new_string`; the tool fails if `old_string` is not unique. This is cheaper in tokens and far
  less likely to clobber code the model did not look at.
- **Tool descriptions are long and prescriptive.** Much of a coding agent's "prompt engineering"
  lives in tool descriptions: when to use grep versus reading a file, why to read before editing.

Ours: `mcp_server/server.py` exposes `read_file`, `edit_file` (search and replace), `write_file`,
`list_dir`, `search_code`, `run_command`. We will write descriptions the way the real tools do.

### 11.3 The system prompt carries the operating manual

The system prompt tells the model how to behave as an engineer: read before editing, prefer
minimal diffs, run tests, do not commit unless asked, how to format output for a terminal. It also
injects **environment context**: working directory, OS, git status, date, and the contents of a
project instructions file.

Ours: `graph/prompts.py`. We will version it and watch how each change moves the eval pass rate.

### 11.4 Project memory: the instructions file

Claude Code reads `CLAUDE.md`; Cursor reads `.cursorrules`; Codex reads `AGENTS.md`. The pattern is
identical: a Markdown file in the repo, appended to the system prompt, where humans write the rules
the agent should follow for this codebase (build commands, conventions, forbidden directories).
Cheap, transparent, version-controlled memory.

Ours: the agent will look for `AGENTS.md` in the target repo and include it in the system prompt.

### 11.5 Permissions: the model proposes, the harness decides

Every serious coding agent separates *deciding* (model) from *executing* (harness) and inserts a
policy layer between them. Claude Code classifies tools as read-only or mutating, asks the user
before mutating actions, supports allow and deny lists, and has modes (plan-only, auto-accept
edits, bypass). Sandboxing of shell commands and path restrictions sit underneath.

Ours: `sandbox/` implements the jail and command policy; the CLI implements the confirmation gate
and `--yes`. Plan mode maps to running only `retrieve_context` and `plan` and stopping.

### 11.6 Context management: the window is the scarce resource

Long sessions overflow the context window. Production agents handle this with:

- **Truncation of tool output**: a 10,000-line test log is cut with a note saying how much was cut.
- **Compaction**: when the conversation nears the limit, the model summarises it and the summary
  replaces the history.
- **Retrieval instead of dumping**: search tools return matching lines, not whole files.
- **Prompt caching**: the static prefix (system prompt, tool schemas) is cached server-side so each
  turn only pays for the new tokens.

Ours: output caps in `sandbox/`, RAG in `rag/`, and a `compact` step once the message list grows
past a threshold. Free-tier token budgets make this pressure very real for us.

### 11.7 Planning and to-do tracking

Claude Code has a plan mode (explore, propose, wait for approval) and a to-do list tool the model
uses to keep itself on track over long tasks. Both are external structure that compensates for the
model losing the thread.

Ours: the `plan` node with structured output is the to-do list; `reflect` revises it.

### 11.8 Sub-agents

For broad searches or independent sub-tasks, Claude Code spawns a child agent with its own context
window and a narrower tool set, then keeps only its final report. This protects the parent's
context from thousands of lines of search results.

Ours: stretch goal. LangGraph supports this as a subgraph invoked from a node.

### 11.9 Extensibility: MCP and hooks

MCP lets users plug arbitrary tool servers into the agent without changing its code. Hooks run
user-defined shell commands before or after tool calls (lint on every edit, block certain
commands). Both are ways to customise behaviour outside the model.

Ours: we are MCP-native from the start, on both sides of the protocol.

### 11.10 Observability and evals

Vendors run these agents against benchmarks such as SWE-bench (real GitHub issues with hidden
tests) and trace every run. Prompt or tool changes ship only if the number moves.

Ours: LangSmith traces plus `evals/` as a miniature SWE-bench.

### Map at a glance

| Production concern | Where it lives in coder-agent |
|---|---|
| Agentic loop | `graph/build.py` (`act <-> tools`) |
| General tool primitives | `mcp_server/server.py` |
| System prompt and environment context | `graph/prompts.py` |
| Project instructions file | `AGENTS.md` lookup in `plan` node |
| Permissions and sandbox | `sandbox/`, CLI confirmation gate |
| Output truncation and compaction | `sandbox/local.py`, `compact` node |
| Plan mode and to-do list | `plan` and `reflect` nodes |
| Sub-agents | stretch: LangGraph subgraph |
| Extensibility | MCP client in `tools/client.py` |
| Evals and tracing | `evals/`, LangSmith |

---

## Glossary

- **Agent**: LLM in a loop with tools and a stop condition.
- **Tool calling**: model emits a structured request; our code executes it.
- **LangGraph**: library for expressing agents as state graphs with checkpointing.
- **Node / edge / state**: function / transition / shared typed dict.
- **Checkpointer**: saves state after each node; enables resume and human-in-the-loop.
- **MCP**: protocol for exposing tools to any agent client; servers speak JSON-RPC over stdio/HTTP.
- **FastMCP**: decorator-based way to write an MCP server in Python.
- **RAG**: retrieve relevant chunks, then generate.
- **Embedding**: vector representation of text; similar meaning gives nearby vectors.
- **Chroma**: local vector database.
- **LangSmith**: tracing and evaluation platform for LLM apps.
- **Structured output**: forcing the model to answer with JSON that matches a schema.
