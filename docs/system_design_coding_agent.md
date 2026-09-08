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
