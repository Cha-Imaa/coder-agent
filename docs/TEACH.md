# Teach

One entry per finished step of `docs/PLAN.md`. Each entry answers four questions: what we built,
why it is built that way, how production coding agents do the same thing, and how to check it
yourself. Read it top to bottom and you should be able to explain every file in `src/`.

---

## Step 0.1 — Project layout, `pyproject.toml`, `uv`, `.env.example`

**What we built.** A Python package in `src/coder_agent/` with one sub-package per layer of the
architecture (`graph/`, `mcp_server/`, `tools/`, `sandbox/`, `rag/`, `ui/`). `pyproject.toml`
declares dependencies and a `coder` command-line entry point. `uv` manages the virtualenv and a
lockfile. `.env.example` lists every secret and setting the project reads, with empty values.

**Why this way.**
- *`src/` layout.* Tests import the installed package, not the folder next to them, so a missing
  `__init__.py` or a wrong import path fails in tests instead of in production.
- *One folder per layer.* The plan's architecture table maps one-to-one onto folders, so you can
  find any responsibility by name.
- *`uv` and a lockfile.* Reproducible installs. `uv sync --extra dev` gives anyone the same
  environment in seconds.
- *`.env.example` committed, `.env` ignored.* Documents the config surface without leaking keys.

**How the real tools do it.** Claude Code, Aider and OpenHands all read configuration from
environment variables plus a settings file, and all separate "the agent" from "the tools it
calls" at the package level. Our folder split is the same idea in miniature.

**Check it.**
```bash
uv sync --extra dev
uv run python -c "import coder_agent; print('ok')"
```

---

## Step 0.2 — Typed settings and LLM factory with fallback

**What we built.** `config.py` defines a `Settings` class using pydantic-settings. Every field
has a type and a default and is read from an environment variable prefixed `CODER_` or from
`.env`. `llm.py` has one function, `get_llm()`, that returns a chat model built from the
`CODER_MODEL` string and wrapped so that failures fall through to `CODER_FALLBACK_MODEL`.

**Why this way.**
- *Typed settings instead of `os.getenv`.* A typo in a variable name or a non-integer timeout is
  caught at startup with a clear error, not deep inside the agent loop.
- *`init_chat_model("groq:llama-3.3-70b-versatile")`.* LangChain's provider-agnostic constructor.
  The `provider:model` string picks the integration package and the model at once, so switching
  from Groq to Gemini is a config change, not a code change.
- *`with_fallbacks([...])`.* Free tiers rate-limit aggressively. When the primary raises, usually
  an HTTP 429, the same call is transparently retried on the fallback. The rest of the code never
  knows which provider answered.
- *`temperature=0`.* Coding agents want determinism. Creativity here means flaky edits.

**How the real tools do it.** Every production agent has a model abstraction layer with retry
and fallback. Claude Code falls back to a different model when a request is refused or a model is
unavailable. Aider lets you name a "weak model" for cheap tasks and a main model for edits. Our
two-model setup is the smallest version of that pattern.

**Check it.**
```bash
uv run python scripts/smoke_llm.py
```
One reply should stream back. With `LANGSMITH_TRACING=true` and a key set, the call appears as a
trace in the LangSmith project.

---

## Step 1.1 — FastMCP server with six tools

**What we built.** `mcp_server/server.py` is a standalone process that speaks the Model Context
Protocol over stdio. It exposes six tools: `read_file`, `edit_file`, `write_file`, `list_dir`,
`search_code`, `run_command`. The repository it operates on is passed as a command-line argument
when the process starts.

**Key concepts.**
- *MCP.* A standard protocol for exposing tools to language models. The server advertises tools
  with a name, a description and a JSON schema for the arguments. Any client that speaks MCP,
  including an IDE or an inspector, can discover and call them. It is JSON-RPC under the hood.
- *stdio transport.* The client launches the server as a child process and exchanges messages
  over the child's stdin and stdout. No ports, no network, nothing to secure.
- *Tool descriptions are prompt text.* The docstring of each tool is sent to the model verbatim.
  That is why they are written as instructions ("Read a file before editing it", "include a few
  surrounding lines to make it unique") rather than as documentation for humans.

**Why this way.**
- *Few general tools.* Six primitives cover reading, writing, navigating, searching and running.
  Task-specific tools would multiply and the model would pick wrong ones.
- *`edit_file` is search-and-replace, not whole-file rewrite.* The model sends the exact old text
  and the new text. The tool refuses if the old text is missing or occurs more than once. This
  makes edits cheap in tokens and impossible to apply to the wrong place.
- *`read_file` returns `LINE|content`.* Line numbers let the model ask for a range next time and
  refer to specific lines in its reasoning.
- *Errors are returned as strings, not raised.* A raised exception would kill the tool call. A
  string starting with `ERROR:` goes back into the model's context so it can correct itself.
- *Repo root from argv.* The client controls which repository the server can touch. The model
  cannot change it because nothing in the tool interface lets it.

**How the real tools do it.** Claude Code's tool set is almost exactly this list: Read, Edit,
Write, Glob, Grep, Bash. Its Edit tool is also exact-string replacement with a uniqueness check,
and its Read tool also returns numbered lines. Claude Code additionally consumes external MCP
servers configured by the user, which is the same protocol we are speaking here.

**Check it.**
```bash
uv run python -m coder_agent.mcp_server.server .
```
The process waits for JSON-RPC on stdin. Stop it with Ctrl+C. Step 1.4 will use MCP Inspector to
drive it interactively.

---

## Step 1.2 — Sandbox: path jail, command denylist, timeout, output cap

**What we built.** `sandbox/local.py`, the policy layer every tool calls before touching the
filesystem or the shell. Two functions matter: `resolve_in_repo()` and `run_command()`.

**Key concepts.**
- *The model is untrusted input.* It may hallucinate a path outside the repo, or write a command
  that deletes files. The sandbox assumes this and checks everything.
- *Path jail.* `Path.resolve()` collapses `..` segments and follows symlinks, giving the real
  absolute location. Only then do we check it lies under the repo root. Checking the string
  before resolving would miss `src/../../etc/passwd`.
- *Denylist, not allowlist.* Regular expressions block the obviously destructive: recursive
  deletes, `git push`, `git reset --hard`, package installs, `curl | sh`, fork bombs. This is a
  guardrail against accidents. A real security boundary would be a container, which is the
  Docker mode planned for milestone 5.
- *Timeout.* Any command is killed after `CODER_COMMAND_TIMEOUT` seconds and the result says so.
  Without it a hung test suite hangs the agent forever.
- *Head-and-tail truncation.* Output over 20,000 characters keeps the start and the end and drops
  the middle. Test failures are at the end, the command echo and first errors at the start. The
  middle is the least useful part and the most expensive in tokens.
- *Exit code first.* `CommandResult.as_text()` puts `exit_code=N` on line one because that is the
  fact the model must act on.

**How the real tools do it.** Claude Code has a permission layer between the model's tool call
and its execution: some commands are auto-allowed, some denied, others prompt the user. Its Bash
tool also has a timeout and truncates long output. Our sandbox is that layer with the
"ask the user" option removed for now; milestone 5's human-in-the-loop interrupt will add it.

**Check it.**
```bash
uv run pytest tests/test_sandbox.py -v
```
Fourteen tests cover traversal attempts, blocked and allowed commands, timeouts and truncation.

---

## Step 1.3 — MCP client that loads the tools as LangChain tools

**What we built.** `tools/client.py` with `load_tools(repo)`. It uses `MultiServerMCPClient` from
`langchain-mcp-adapters` to launch our server as a child process, perform the MCP handshake, ask
for the tool list, and wrap every tool in a LangChain `BaseTool`. `tests/test_mcp_tools.py`
exercises the whole path end to end: a real server process, real JSON-RPC over stdio, real files
in a temporary repository.

**Key concepts.**
- *Adapter.* LangGraph does not speak MCP. The adapter converts each MCP tool into the object
  LangGraph's `ToolNode` expects, using the server's JSON schema as the argument schema. From the
  graph's point of view, an MCP tool and a local Python function are indistinguishable.
- *Multi-server.* The client takes a dictionary of servers. Adding a GitHub or web-search server
  later is one more entry, and the model sees one merged tool list.
- *`sys.executable`.* The server is launched with the same Python interpreter running the client,
  so it runs inside the same virtualenv. Hard-coding `python` would pick up whatever is first on
  the PATH.
- *End-to-end tests over stdio.* Unit-testing the tool functions directly would miss schema
  errors, serialization bugs and process-launch problems. Spawning the real server catches all
  three.

**How the real tools do it.** Claude Code, Cursor and VS Code Copilot all act as MCP clients: they
read a config listing servers, launch them, and merge the discovered tools into the model's tool
list. What we wrote is the same client role, aimed at our own server.

**Check it.**
```bash
uv run pytest tests/test_mcp_tools.py -v
```
Eleven tests: the tool list, line-numbered reads, unique and ambiguous edits, file creation,
directory trees, code search, command exit codes, and sandbox errors coming back as text.

---

## Step 1.4 — Listing the tool schemas; MCP Inspector

**What we built.** `scripts/list_tools.py` connects to our server the same way the agent will
and prints what comes back from `tools/list`: the name, the description and the JSON schema of
every tool, with required arguments starred.

**Key concepts.**
- *The schema is generated from the Python signature.* FastMCP turns `path: str`,
  `start_line: int = 1`, `end_line: int | None = None` into a JSON schema with `path` required and
  the others optional. Type hints are therefore part of the prompt: a wrong hint teaches the model
  the wrong argument.
- *What you see is what the model sees.* Nothing in between rewrites the descriptions, so this
  listing is the place to review tool text for clarity and length.

**MCP Inspector.** Anthropic ships a browser UI that does the same and lets you call tools by
hand. It needs Node.js:
```bash
npx @modelcontextprotocol/inspector uv run python -m coder_agent.mcp_server.server .
```
It opens a page listing the six tools; pick `read_file`, enter `README.md`, and the numbered
lines come back. This is the debugging loop you use when a tool misbehaves: call it directly
with the exact arguments the model sent, outside the agent.

**How the real tools do it.** Claude Code has a `/mcp` command that lists connected servers and
their tools; Cursor shows the same in its settings panel. Both are `tools/list` rendered nicely.

**Check it.**
```bash
uv run python scripts/list_tools.py .
```
Six tools; `edit_file` has three required arguments, `list_dir` none.

---

## Step 2.1 — Graph state, `plan` and `act` nodes, `ToolNode`, `finish`

**What we built.** The agent loop itself, in `src/coder_agent/graph/`:

```
START -> plan -> act -> (tools -> act)* -> finish -> END
```

- `state.py`: the `AgentState` TypedDict. Inputs (`task`, `repo`), the model conversation
  (`messages`), the current `plan`, counters (`iteration`, `steps`) and outputs (`status`,
  `summary`).
- `prompts.py`: the system prompts for planning, acting and (later) reflecting, in one file.
- `nodes.py`: `make_plan_node`, `make_act_node`, `finish`, and the router `route_after_act`.
- `build.py`: `build_graph(llm, tools)` wires nodes and edges and compiles the graph.
- `tests/fakes.py`: a `ScriptedLLM` that replays canned answers, so the graph is tested with no
  API key. `tests/test_graph.py`: ten tests, one of which drives the real MCP server.

**Key concepts.**
- *State is a dict with reducers.* A LangGraph node receives the full state and returns only the
  keys it changed. For most keys the new value replaces the old. `messages` is annotated with
  `add_messages`, so returning `{"messages": [msg]}` appends instead of replacing. That one
  annotation is what makes a multi-turn conversation accumulate across nodes.
- *Nodes are closures over dependencies.* `make_act_node(llm, tools)` returns the node function.
  The graph never imports a concrete model, so tests inject a fake and production injects Groq.
  This is plain dependency injection; it is the reason the whole loop is testable offline.
- *ReAct in two nodes.* `act` asks the model; if the answer carries `tool_calls`, the conditional
  edge sends it to `tools`, whose `ToolMessage`s flow back to `act`. If the answer is prose, the
  loop is over. The router is a pure function of state, which makes it trivially unit-testable.
- *`ToolNode`.* LangGraph's prebuilt node reads the tool calls on the last AI message, runs each
  one (sync or async), and appends one `ToolMessage` per call, matched by `tool_call_id`. With
  `handle_tool_errors=True` a raising tool becomes an error message the model can react to,
  instead of a crashed run.
- *Plan is state, not history.* The plan lives in `state.plan` and is formatted into the system
  prompt on every `act` call. Storing it as a message would freeze it into the conversation;
  keeping it in state lets `reflect` (milestone 3) replace it without rewriting history.
- *Two safety counters.* `iteration` counts plan-act-test cycles and will be bounded by
  `max_iterations`. `steps` counts model calls inside `act` and is bounded by `max_steps`, so a
  model that keeps calling tools without converging is cut off and the run ends as `gave_up`.
- *MCP results are content blocks.* A `ToolMessage` from an MCP tool has `content` as a list of
  `{"type": "text", ...}` blocks, not a string, because MCP tools may return images or resources
  too. Use `message.text` to get the joined text.

**Why this way.**
- *Separate plan call.* A plan is cheap (no tools bound, short output), gives the user something
  to read before any file changes, and gives the model a chance to think about verification
  before it starts reading files.
- *Fake LLM in tests, real tools in one test.* Routing bugs are logic bugs; they should fail fast
  and deterministically. One end-to-end test with the real MCP server proves the async tools,
  the content-block format and the file writes all work together.

**How the real tools do it.** Every production coding agent is this loop. Claude Code's core is
"call model, execute tool calls, append results, repeat until the model stops calling tools",
with a step budget. Aider separates a planning conversation ("architect" mode) from an editing
one, like our `plan` and `act`. OpenHands models the loop as an event stream where each action
produces an observation, which is what our `messages` list is.

**Check it.**
```bash
uv run pytest tests/test_graph.py -v
```
Ten tests: three for the router, six for the whole graph with a scripted model (tool results fed
back, errors turned into messages, step cap ends the run), one driving the real MCP server.

---

## Steps 2.2 and 2.3 — Prompts as code; `coder run` streams the graph

**What we built.**
- `graph/prompts.py` holds the three system prompts (plan, act, reflect). The act prompt is a
  template with `{task}` and `{plan}` slots, filled on every call.
- `cli.py` is the `coder` command. `coder run <repo> "<task>"` loads the tools, builds the graph
  with the configured model, and streams updates. `--model`, `--max-iterations` and `-v` override
  settings for one run. Exit code is 0 on `passed`, 1 otherwise, so scripts and evals can use it.
- `ui/render.py` renders each node's update: the plan in a panel, each tool call on one line
  with trimmed arguments, tool results as a short preview (full text with `-v`), edits as a
  coloured unified diff, and the final status and summary in a panel.
- `edit_file` now returns a unified diff after `OK: edited`. The model uses it to confirm the
  edit landed where intended; the UI uses it to show the change without re-reading the file.

**Key concepts.**
- *`stream_mode="updates"`.* `graph.astream(...)` yields one dict per node execution, keyed by
  node name, containing just the patch that node returned. That is exactly the granularity a UI
  wants: "plan finished, here is the plan", "act finished, here are its tool calls". Other modes
  exist: `values` gives the whole state after each step, `messages` gives token-by-token chunks.
- *The UI reads state, not the model.* Nothing in the renderer knows about prompts or providers.
  If a node's patch shape changes, one method in `Renderer` changes. This is the payoff of putting
  behaviour in the graph: the CLI, the eval runner and a future chat mode all consume the same
  stream.
- *Two audiences for tool output.* The model sees the full result (it needs it). The human sees
  the first dozen lines (they need to follow along, not read everything). Trimming happens in
  the renderer only, never in state.
- *Prompt rules mirror tool contracts.* "Copy old_string exactly, without the line-number
  prefix" exists because `read_file` numbers lines and `edit_file` needs exact text. Every rule
  in the act prompt is there because a tool would otherwise fail in a specific way.

**How the real tools do it.** Claude Code prints each tool call as a one-liner and collapses
the result unless you expand it; edits are shown as diffs. Aider shows diffs and the test output
after each edit. Our renderer follows the same compact-by-default convention.

**Check it.**
```bash
uv run pytest tests/test_cli.py -v
uv run coder --version
uv run coder run --help
```
With `.env` filled in, a real run:
```bash
uv run coder run path/to/small/repo "add a function is_even(n) to utils.py with a test"
```
