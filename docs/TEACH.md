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
- *`init_chat_model("groq:openai/gpt-oss-120b")`.* LangChain's provider-agnostic constructor.
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

---

## Step 0.3 — Smoke test and the first trace (and a model that vanished)

**What happened.** The first real call failed: Groq had retired `llama-3.3-70b-versatile`, the
default model chosen when the project started. The call still returned an answer, because
`with_fallbacks` sent it to Gemini. Only the LangSmith trace revealed which provider actually
replied: the root run `RunnableWithFallbacks` contained a `ChatGroq` child marked *error* and a
`ChatGoogleGenerativeAI` child marked *success*.

**What we changed.**
- Default model is now `groq:openai/gpt-oss-120b` (131k context, tool calling verified). The list
  of models an account can use comes from the API, not from documentation:
  `Groq(api_key=...).models.list()`.
- `scripts/smoke_llm.py` forces UTF-8 on stdout. Windows terminals default to cp1252 and crash on
  characters such as the non-breaking hyphen this model likes to emit.

**Key concepts.**
- *Fallbacks hide failures by design.* That is what you want in production and what you do not
  want while developing: a silent fallback to a slower or weaker model changes results without
  telling you. Observability is how you keep the benefit without the blindness.
- *Traces are trees.* A LangSmith run is nested: the graph, then each node, then each model call,
  then each tool call, each with inputs, outputs, latency and token counts. The `is_root=True`
  filter in the client lists top-level runs; opening one shows the children.
- *Free-tier limits are per model and per minute.* Groq's `qwen/qwen3.6-27b` rejected a small
  request because its output-tokens-per-minute cap (1000) was below the default `max_tokens`.
  Rate limits are part of model selection, not an afterthought.

**How the real tools do it.** Claude Code and Cursor both log every model call with provider,
model, tokens and latency, and surface fallbacks in their status output. LangSmith gives us that
for free with two environment variables; production teams use the same product or OpenTelemetry.

**Check it.**
```bash
uv run python scripts/smoke_llm.py
```
Then open the `coder-agent` project at https://smith.langchain.com and expand the newest run.

---

## Step 2.4 — First real end-to-end run

**What we did.** Pointed `coder run` at a toy repository (one module, one pytest test) with the
task "add `is_even(n)` and a test, run the tests". The agent listed the directory, read both
files, edited both with `edit_file`, hit one ambiguous-edit error and recovered by re-reading,
ran pytest (2 passed) and stopped with a summary. Exit code 0. A second task (`clamp`) passed the
same way. Full traces are in LangSmith.

**What the run taught us, and what we changed.**
- *Invented argument names are silently accepted.* The model called
  `read_file(line_start=1, line_end=400)` and `list_dir(depth=2)`. Neither name exists, MCP and
  pydantic both ignore unknown keys, so the tools ran with defaults and the model never knew.
  For a small file that is harmless; for "read lines 400-450 of a 2000-line file" it returns the
  whole file and burns the context window. `tools/client.py` now wraps every tool in
  `strict_arguments`, which returns `ERROR: unknown argument(s) [...]. Valid arguments: [...]`
  so the model corrects itself on the next step.
- *Error-as-message works.* `edit_file` refused an `old_string` of `"\n"` (six matches). The
  error text went back as a `ToolMessage`, the model re-read the file and chose a unique anchor.
  No code of ours handled that; the loop design did.
- *Fallbacks fire mid-run.* Halfway through the first run Groq rate-limited and the next call
  went to Gemini, which warned eight times that it was dropping Groq's reasoning blocks. Correct
  behaviour, ugly output: the CLI now sets that logger to ERROR. The run continued and passed,
  which is the point of the fallback.
- *Two processes, one terminal.* FastMCP logs every request at INFO to stderr, interleaving with
  the Rich UI. The server now starts with `log_level="WARNING"`.
- *Prompts leak habits.* The plan ended with "commit and push and open a pull request". The
  planner prompt now says the user commits; the act prompt forbids `git commit`, `git push` and
  installs, in addition to the sandbox denylist that would have blocked them anyway.
- *Do not print the summary twice.* The final model message is already shown; `finish` now
  prints a one-line coloured rule with the status.

**How the real tools do it.** Every one of these is a known production issue. Claude Code
validates tool inputs against the schema and returns the validation error to the model. Aider
and OpenHands both feed edit failures back as text. Silent fallbacks with observability, and
prompt rules that duplicate hard guardrails, are standard practice: the prompt makes the model
behave, the guardrail makes sure it cannot misbehave.

**Check it.**
```bash
uv run pytest tests/test_mcp_tools.py -k "unknown or diff" -v
uv run coder run <toy repo> "add a function is_even(n) to utils.py with a test, run the tests"
```
Open the run in LangSmith: the root run is the graph, children are `plan`, `act`, `tools`, and
each `act` contains one `RunnableWithFallbacks` with the provider that actually answered.

---

## Steps 3.1 and 3.2 — `run_tests`, `reflect`, and the iteration loop

**What we built.** The graph now verifies its own work and retries:

```
START -> prepare -> plan -> act -> (tools -> act)* -> run_tests -> finish -> END
                      ^                                   |
                      +---------- reflect <-- failed & iteration < max_iterations
```

- `graph/testing.py`: `detect_test_command(repo)` looks for `pytest.ini`, `conftest.py`,
  `pyproject.toml`, `package.json` scripts, `go.mod`, `Cargo.toml`, or a `tests/` folder and
  returns the shell command. `--test-cmd` on the CLI overrides it.
- `prepare` node: detects the command once, before planning, unless the caller supplied one.
- `run_tests` node: runs that command through the sandbox and sets `tests_passed`,
  `test_output` and `status`. No model call.
- `reflect` node: appends the failing output to the conversation as a human turn. No model call.
- `route_after_act` now sends a prose answer to `run_tests` instead of `finish`.
  `route_after_tests` goes to `finish` on green or when `max_iterations` is reached, otherwise to
  `reflect`, which leads back to `plan` with the failure attached.

**Key concepts.**
- *The verdict is not the model's.* The model may say "all tests pass"; only `run_tests` decides.
  It is deterministic, costs no tokens, and cannot be skipped by a model that forgets to run the
  failing test. This separation is what makes the pass rate in the evals trustworthy.
- *Two loops, two budgets.* The inner loop (`act` ⇄ `tools`) is bounded by `max_steps`; the
  outer loop (`plan` → ... → `run_tests` → `reflect`) by `max_iterations`. A run can end as
  `passed`, `failed` (iterations exhausted, tests still red) or `gave_up` (step cap, model never
  stopped calling tools).
- *Reflection is data, not a model call.* Some agents ask the model to "reflect" in a separate
  prompt. We simply show it the failure and re-plan; the plan prompt includes the failure too.
  The history stays intact, so the model can see the edit that did not work and avoid repeating
  it. Cheaper, and easier to inspect in a trace.
- *"Not verified" is a distinct outcome.* With no detectable test command the run is accepted but
  the summary says so. Silently reporting success would corrupt the eval numbers later.
- *Detection is a rule list, first match wins.* Python first because it is the primary target;
  `package.json` only counts when its `test` script is real (npm's default is a placeholder that
  exits 1).

**How the real tools do it.** SWE-agent and OpenHands run the repository's tests as a separate
harness step and use that, not the model's claim, as the success signal. Aider runs the test
command after every edit and feeds failures back automatically (`--auto-test`). Claude Code
leaves running tests to the model but its Bash tool returns the exit code prominently, as ours
does. Our `run_tests` is the harness-style verdict; the model can still run tests itself during
`act`, which it does.

**Check it.**
```bash
uv run pytest tests/test_testing_loop.py -v
```
Fourteen tests: detection for six ecosystems, the node on a real failing and passing pytest
repo, routing, a full two-iteration loop where the second iteration fixes the bug, giving up at
`max_iterations`, and a caller-supplied command. Then a real run on a repo with a broken function:
```bash
uv run coder run <repo> "The test suite has a failing test. Fix the bug without changing the tests."
```
The last lines are `tests: passed` and a green `passed` rule.

---

## Step 3.3 — Context management: compaction and summarisation

**What we built.** `graph/context.py`, called by the `act` node before every model call.

1. `compact_tool_outputs`: every tool result except the most recent six is cut to its first
   400 characters plus a stub saying how many characters were dropped and that the tool can be
   called again. The message keeps its id and position.
2. `summarize_if_needed`: if the conversation is still over `context_budget_tokens` (60k), the
   middle is replaced by one model-written summary. The first message (the task) and the last
   eight messages are kept verbatim.
3. `manage_context` runs the two in that order: the free one first, the one that costs a model
   call only if needed. When anything changed, `act` writes the rebuilt history back to state.

**Key concepts.**
- *Tool outputs are the context hog.* A coding session is mostly file reads and test logs. The
  model needs them when it acts on them; afterwards, knowing "I read `utils.py`" is enough, and
  it can re-read if it must. Stubbing rather than deleting keeps the record of what happened.
- *Never orphan a tool result.* Provider APIs reject a `ToolMessage` whose `tool_call_id` does not
  match a preceding AI message. Compaction edits in place, so pairing is untouched.
  Summarisation moves its cut forward past any `ToolMessage` so the tail always begins with an
  AI or human turn. The tests check this invariant explicitly.
- *`add_messages` cannot edit history in place.* It appends, or replaces by id. To rewrite the
  conversation the node returns `RemoveMessage(id=REMOVE_ALL_MESSAGES)` followed by the new
  list, in one patch. LangGraph applies the removal then the appends, so the order is preserved.
- *Approximate token counting is enough.* `count_tokens_approximately` estimates from character
  counts without a tokenizer. The budget sits far below the window (60k of 131k), so a 20% error
  does not matter. Exact counting would need a tokenizer per provider.
- *Summaries are written for the agent, not for humans.* The summary prompt asks for file paths,
  exact error text and exit codes, in a fixed order. A prose summary ("the agent explored the
  code") would lose exactly what the model needs to continue.

**How the real tools do it.** Claude Code "auto-compacts" when the context nears its limit: it
summarises the conversation with the model and continues from the summary, which is our step 2.
It also truncates long tool results at read time. Aider keeps a "repo map" instead of raw file
contents and drops old chat turns. OpenHands has a condenser component with the same two
strategies: drop-or-stub old observations, then LLM-summarise.

**Check it.**
```bash
uv run pytest tests/test_context.py -v
```
Nine tests: stubbing keeps recent outputs and ids, idempotence, no summary under budget, the
summary replaces the middle and sees only the middle, the cut never orphans a tool result, and
the `act` node writes the compacted history back so the next model call sees it.

---

## Step 3.4 — Telemetry ledger: what every run cost

**What we built.** `telemetry/ledger.py` and a `usage` field in the graph state.

- `usage_from_message(node, ai_message)` reads `usage_metadata` (input and output tokens) and
  `response_metadata["model_name"]` from a model reply. `plan` and `act` return it in their patch.
- `merge_usage` is the reducer on `state.usage`: it sums calls and tokens per node and counts
  how many calls each model answered. No node reads the total; the reducer builds it.
- `Ledger` writes one row per run to `~/.coder-agent/ledger.sqlite` (`runs`) plus one row per node
  (`node_usage`): status, iterations, steps, wall time, test verdict, tokens, models, tags.
- `coder run` ends with a footer such as
  `9 model calls · 13,856 in / 1,202 out tokens · 57s · 1 iteration(s) · gemini-2.5-flash, openai/gpt-oss-120b`
  and `coder stats` prints pass rate, averages, tokens by node and the last runs.

**Key concepts.**
- *Reducers do the accounting.* Any state key can carry a merge function, not only `messages`.
  Each model call adds its own small dict; LangGraph folds them. A node that later runs in
  parallel branches would still be counted correctly, which a global counter would get wrong.
- *Two observability layers.* LangSmith holds full traces for debugging one run. The ledger holds
  numbers for aggregating many runs. Traces answer "why did this run fail"; the ledger answers
  "what is the pass rate and what does a run cost". The eval harness and the figures read the
  ledger.
- *Record which model actually answered.* The first ledgered run shows both `openai/gpt-oss-120b`
  and `gemini-2.5-flash`: Groq rate-limited mid-run and the fallback took over. Without the
  per-model count that would be invisible, and a later model comparison would be comparing
  mixtures.
- *Where the tokens go.* In that run the `act` node used 13,598 input tokens across eight calls
  and `plan` used 258. Input dominates because the whole conversation is re-sent on every call.
  That is the number context management (step 3.3) is there to bound, and the "cost profile"
  figure in the plan will show it per node.
- *SQLite, not a JSON file.* Appending safely from an interrupted run, querying with `GROUP BY`,
  and being a single copyable file are worth more than human readability here.

**How the real tools do it.** Claude Code tracks tokens per session and prints a cost summary on
exit (`/cost`). Aider prints tokens and dollar cost after every message. Both keep the count in
the client, from the provider's usage fields, exactly as `usage_from_message` does. Benchmark
harnesses (SWE-bench, Aider's polyglot) store per-task JSON records that are the equivalent of
our `runs` rows.

**Check it.**
```bash
uv run pytest tests/test_telemetry.py -v
uv run coder stats
```
Seven tests: extraction, merging, accumulation through the graph with a scripted model whose
replies carry usage metadata, ledger write and summary, idempotence by run id, and the `stats`
command against a temporary ledger.

---

## Step 4.1 — The in-house eval suite: twelve tasks with hidden tests

**What we built.** `evals/suite/` holds twelve small repositories, each a benchmark task, and
`evals/tasks.py` (in `src/coder_agent/evals/`) loads and grades them.

A task is a directory with four parts:

| Part | Who sees it | Purpose |
|---|---|---|
| `task.toml` | runner | `id`, `category`, the `prompt` given to the agent, `notes` on why the task is in the suite |
| `repo/` | agent | the starting repository, copied fresh for every run |
| `hidden_tests/` | grader | copied into `repo/tests/` after the run; exit code 0 is a pass |
| `solution/` | suite tests | a reference fix, used only to prove the task is solvable |

Categories and counts: fix-bug (3), add-feature (3), refactor (2), add-test (2), multi-file (2).
`load_suite()` returns `EvalTask` objects; `materialise`, `install_hidden_tests` and
`apply_solution` do the file copying; `grade()` runs pytest through the sandbox.

**Key concepts.**
- *Hidden tests are the grade.* The agent sees the repo and the prompt, never `hidden_tests/`.
  So it cannot pass by editing an assertion, and a fix that special-cases the one visible input
  fails the hidden cases. Where the prompt asks the agent to edit an existing test file
  (`refactor-config-dataclass`), the hidden file has the same name and replaces it.
- *Every task is validated in both directions.* `tests/test_eval_suite.py` materialises each task
  twice: hidden tests must fail on the untouched repo (doing nothing scores zero) and pass after
  `solution/` is overlaid (the task is solvable and the grader works). Writing this test caught a
  real mistake: one planted bug was equivalent to the original code, so no test could ever
  detect it.
- *Add-test tasks are graded by mutation.* "Write tests for `mathx.py`" cannot be graded by
  running the agent's tests, since an empty test file passes. The hidden grader copies the repo,
  plants one bug at a time (`is_prime(1)` returns True, `gcd` loses its sign, `fibonacci(0)` is
  off by one) and requires the agent's tests to fail on each mutant while passing on the real
  module. That measures whether the tests check anything, not whether they exist.
- *Each category exercises a different skill.* fix-bug needs reading a failure; add-feature needs
  following a spec with exception types; refactor needs preserving golden output while changing
  structure; multi-file needs editing three files with different error contracts; add-test needs
  thinking about boundaries. A pass rate per category tells us where the agent is weak.
- *Fixtures are excluded from lint.* `ruff` would "fix" the mutable-default bug the agent is
  supposed to find. `pyproject.toml` excludes `evals/suite` for that reason.

**How the real tools do it.** SWE-bench is the same shape at scale: a repository at a given commit,
a natural-language issue, and `FAIL_TO_PASS` tests hidden from the agent that must fail before and
pass after the patch, plus `PASS_TO_PASS` tests that must keep passing. Its validation step, running
the gold patch to confirm the tests flip, is our `test_hidden_tests_fail_before_and_pass_after`.
Aider's polyglot benchmark packages Exercism exercises with hidden tests. Mutation testing as a
grade for tests is how `mutmut` and `cosmic-ray` measure test-suite quality.

**Check it.**
```bash
uv run pytest tests/test_eval_suite.py -v
```
Seventeen tests: shape of the suite, malformed spec rejection, copy independence, hidden file
replacement, and the fail-before/pass-after check for each of the twelve tasks (about 45 seconds,
since it runs pytest in 24 temporary repos). To read a task:
```bash
cat evals/suite/multi-file-cart-discount/task.toml
```

---

## Step 4.2 — `run_evals.py`: the runner that turns the suite into a number

**What we built.** Two modules and a script.

- `src/coder_agent/agent.py` — `run_agent(repo, task, ...)`: the one function that loads the
  MCP tools, builds the graph, streams it, and writes a `RunRecord` to the ledger. `coder run`
  now calls it with the Rich renderer as its `on_update` hook; the eval runner calls it with no
  hook. Before this step that logic lived inside the CLI.
- `src/coder_agent/evals/runner.py` — `run_task` materialises one task into a temporary
  directory, hands the copy and the prompt to an *agent callable*, then installs the hidden
  tests and grades. `run_suite` does that for a list of tasks, sequentially, and collects a
  `SuiteResult` with per-category rows, a Markdown table, and a JSON file under
  `evals/results/` stamped with the commit, platform and loop settings.
- `evals/run_evals.py` — the CLI: `--category`, `--task` (repeatable), `--model`,
  `--max-iterations`, `--pause` between tasks, `--keep-workdirs`, and `--agent graph|solution|noop`.

**Key concepts.**
- *The agent is a parameter.* `Agent = Callable[[Path, str], Awaitable[dict]]`. The real one,
  `graph_agent`, wraps `run_agent`. Two others exist only to test the harness: `solution_agent`
  overlays the reference fix and must score 100%; `noop_agent` does nothing and must score 0%.
  If either check fails, the grader is broken and no pass rate means anything. Later ablations
  (no retrieval / BM25 / dense / hybrid) are just more agent callables; the grading never changes.
- *Grade after, never during.* Hidden tests are copied in only after the agent returns, and a
  test proves the agent cannot see them. A second test has the agent delete every visible test
  file and claim success: the runner records the claim (`agent_status="passed"`) and the truth
  (`passed=False`) side by side. That gap is itself a useful metric: how often the agent
  believes it is done when it is not.
- *A crash is a data point, not an abort.* `run_task` catches everything the agent raises,
  stores the traceback in `error`, and still grades the repo. One rate-limit blow-up on task 7
  must not throw away tasks 1 to 6.
- *Sequential, with a pause.* Free-tier limits are per minute. A parallel runner would spend
  its quota on 429 responses and make the numbers depend on the scheduler. `--pause` is the
  simplest control that keeps runs comparable.
- *Provenance in the file.* Each results JSON records the model, the git commit, the Python
  version and the iteration and step caps. A number without those cannot be reproduced, and a
  README table built from it cannot be trusted six commits later.

**First numbers, and what they taught.** The first full run
(`evals/results/20260909-185603-openai-gpt-oss-120b.json`):

| Category | Tasks | Passed | Errors | pass@1 | Avg tokens |
|---|---|---|---|---|---|
| fix-bug | 3 | 1 | 2 | 33% | 3,289 |
| add-feature | 3 | 3 | 0 | 100% | 28,984 |
| refactor | 2 | 0 | 2 | 0% | 0 |
| add-test | 2 | 2 | 0 | 100% | 19,222 |
| multi-file | 2 | 0 | 2 | 0% | 0 |
| total | 12 | 6 | 6 | 50% | 11,272 |

Every task the agent actually ran, it solved in one iteration: six for six, about 135k tokens
in total, the largest single task 53k. The other six never got a model reply. Groq's free tier
allows 200,000 tokens per day for `gpt-oss-120b`, and six tasks used it. The fallback to Gemini
fired, and Gemini's free tier allows 20 requests per day for `gemini-2.5-flash`, which the
earlier fallbacks had already spent. Both 429s are in the `error` field of each result.

Three lessons went straight into the design:
- *Errors are not failures.* The table has an `Errors` column and the JSON an `errors` count.
  A pass rate of 50% with six quota crashes says nothing about the agent; "6/6 graded, 6 not
  run" does. The pass rate still counts errors as fails, so nobody can hide crashes.
- *Rerun what crashed, keep what ran.* `--rerun-errors <results.json>` runs only the errored
  tasks and merges them into the earlier file, so one configuration's number can be completed
  across quota windows without paying for the tasks that already have a verdict.
- *Budget is a first-class constraint.* 200k tokens a day means roughly eight to ten tasks a day
  on Groq alone, so the eval loop must be frugal: context management (step 3.3) is not a nicety,
  and milestone 6's retry-with-backoff and a third provider (Ollama, milestone 7) are what make
  a full twelve-task run in one sitting possible.

**How the real tools do it.** SWE-bench's harness does the same three moves: build a container
from the task's repo image, apply the agent's patch, run `FAIL_TO_PASS` and `PASS_TO_PASS`, and
write a per-instance JSON report that the leaderboard aggregates. Aider's benchmark runner
(`benchmark/benchmark.py`) creates one directory per exercise, runs the model, then runs the
hidden unit tests and writes a `.aider.results.json` next to each. Both isolate per task, both
grade after the fact with tests the model never saw, and both keep the raw per-task records so
tables can be regenerated. Ours is the same design with a temporary directory instead of a
container; the Docker sandbox in milestone 6 closes that gap.

**Check it.**
```bash
uv run pytest tests/test_eval_runner.py -v
uv run python evals/run_evals.py --agent solution   # must print 100%
uv run python evals/run_evals.py --agent noop       # must print 0%
uv run python evals/run_evals.py --category fix-bug # three real runs
uv run python evals/run_evals.py --rerun-errors evals/results/<file>.json
```
Twelve runner tests, no model needed: both self-checks, the hidden-tests-invisible and
cheating-agent cases, crash capture, per-task isolation, workdir cleanup, token accounting, and
the results file round trip, and merging a rerun. The two self-check commands take about a minute each; they run
pytest in twelve temporary repos.

## Step 4.3 — HumanEval as repository tasks

**What we built.** A second suite under `evals/humaneval/`: the first thirty HumanEval problems,
each turned into the same `repo/ hidden_tests/ solution/` layout the in-house tasks use, so the
runner, the grader and the suite tests did not change at all.

- `src/coder_agent/evals/humaneval.py` — `load_problems` reads the dataset's gzipped JSONL;
  `render_task` writes one problem as a task directory; `is_well_posed` checks it the same way
  `test_eval_suite.py` checks every task (stub fails, canonical passes, visible and hidden);
  `build_slice` walks the dataset in order and keeps the first `count` problems that survive.
- `evals/build_humaneval.py` — downloads the dataset and calls `build_slice`. Its output is
  committed, so nobody needs the network to run evals, and the tasks cannot drift.
- `tasks.py` grows a `SUITES` registry and `load_suites`; `run_evals.py` grows `--suite
  inhouse|humaneval|all`. Results files for the slice are prefixed `humaneval-` and the table
  gets one more category row, so the two kinds of number never mix in one average.

**Key concepts.**
- *Same harness, different corpus.* HumanEval is scored by published leaderboards as one-shot
  completion: signature and docstring in, function body out, `check(candidate)` decides. Our
  agent does not complete prompts; it edits files in a repository and runs tests. Packaging each
  problem as a repo closes that gap: the stub module holds the prompt with a
  `raise NotImplementedError` body, and the agent is told which function and file to fill in.
  The hidden test is HumanEval's own `check`, verbatim, wrapped in one pytest function, so a pass
  here means what a pass means on the leaderboard.
- *The docstring examples are the visible test.* HumanEval gives no tests to the model, but our
  agent's loop needs a failing test to read. Every eligible problem has `>>>` examples in its
  docstring, and `doctest.testmod` over the module turns them into one visible pytest. The stub
  fails it with `NotImplementedError`; the agent iterates until the examples hold; the hidden
  `check` then decides whether it overfitted to the examples. Seventy-six of the 164 problems
  carry examples, and the first thirty of them all doctest cleanly.
- *The generator validates what it emits.* A benchmark task with a wrong docstring example is
  unsolvable, and one whose stub already passes has no signal. `build_slice` renders a candidate,
  runs the before/after check in a temporary copy, and deletes it if either side fails. Nothing
  reaches disk that the suite tests would reject, and the same check runs again in CI on the
  committed files.
- *Deterministic selection.* Dataset order, first thirty eligible. No seed, no sampling. Anyone
  rerunning the script gets the identical slice, and "HumanEval/0 to /29" is a description others
  can reproduce with their own harness.
- *Licences travel with data.* The problems are MIT licensed; `evals/humaneval/README.md` carries
  the notice, because copying a dataset into a repository is redistribution.

**How the real tools do it.** The `human-eval` package itself does exactly one thing: it
concatenates prompt and completion, runs `check` in a subprocess with a timeout, and counts.
Everything above that (turning a completion benchmark into an agent benchmark) is what SWE-bench
did for real repositories, and what Aider's benchmark did for Exercism: put the problem in a
directory with the tests it ships, let the agent run them, then grade with tests it did not see.
OpenAI's later agentic evals and the `evalplus` project both keep the original `check` as ground
truth while adding more inputs, for the same reason we keep it hidden: the visible examples are
too few to trust on their own.

**Check it.**
```bash
uv run pytest tests/test_humaneval.py tests/test_eval_suite.py -q
uv run python evals/run_evals.py --suite humaneval --agent solution   # 30/30
uv run python evals/run_evals.py --suite humaneval --agent noop       # 0/30
uv run python evals/build_humaneval.py                                # rebuild; identical output
uv run python evals/run_evals.py --suite humaneval --pause 5          # the real thing, quota permitting
```
Four packager tests run on a hand-written problem with no dataset or network: layout, stub fails
and canonical passes, a wrong docstring example is skipped, gzipped JSONL parsing. The suite
test now validates 42 tasks before and after the reference solution. A real run costs roughly
five to ten thousand tokens per problem on `gpt-oss-120b`, so the thirty fit inside one day's
Groq quota only if the in-house suite is not run the same day.

## Step 4.4 — `figures.py`: the charts the README is built from

**What we built.** Three PNGs under `docs/figures/`, drawn from the committed results JSON by
one command, plus the two small changes that make the third chart possible.

- `src/coder_agent/evals/figures.py` — the arithmetic (`iteration_curve`, `node_costs`,
  `latest_results`) and, separately, the drawing (`draw_pass_rate`, `draw_iteration_curve`,
  `draw_cost_profile`, `render_all`). Matplotlib is imported inside the drawing functions only.
- `evals/figures.py` — the CLI. With no arguments it takes the newest results file per label and
  skips the `solution`/`noop` self-checks; pass files explicitly to compare specific runs.
- `TaskResult` grows a `usage` field: the per-node token counters the ledger already tracked now
  travel inside the results file too. Older files load with `{}` and the cost profile falls back
  to the ledger, matched on the `task_id` tag every eval run carries.
- `run_task` scrubs the home directory and `site-packages` path out of stored tracebacks,
  because results files are committed and the last one had to be cleaned by hand.
- `matplotlib` is an optional extra (`figures`) and a dev dependency; the agent never imports it.

**Key concepts.**
- *Numbers and pixels are different code.* A chart is only as trustworthy as the arithmetic
  behind it, and arithmetic is easy to unit-test while a PNG is not. So the curve, the per-node
  averages and the "newest per label" rule are plain functions with exact tests; the drawing layer
  gets one test that asserts three files came out. When a figure looks wrong, the bug is in a
  function you can call in a REPL.
- *Figures come from files, not from runs.* `render_all` reads `SuiteResult` objects loaded from
  `evals/results/`. There is no path from "run the model" to "PNG"; the JSON in between is what
  makes a chart reproducible and reviewable. Rerunning with `--rerun-errors` overwrites the same
  file, so the figure and the table always describe one configuration.
- *The iteration curve is cumulative and shares a denominator with the pass rate.* "Solved within
  k iterations" over all tasks, errors included, so its last point *is* pass@1 and the two
  figures cannot disagree. A flat curve says the ceiling was not binding; a curve still climbing
  at `max_iterations` says one more plan/act/test cycle would have bought something.
- *The cost profile averages over runs that happened.* An errored task that never called a model
  has no usage; counting it would halve the average and describe nothing. The subtitle states
  how many runs the average covers, for the same reason the table has an Errors column.
- *Colour by role, in a fixed order.* One categorical palette (blue, orange, aqua, ...) assigned
  in slot order per configuration, a paler step of the same blue for "errored, not graded",
  input and output tokens in slots one and two. No rainbow, no colour that changes meaning between
  charts, every mark also labelled with its value so nothing depends on colour alone.

**How the real tools do it.** Every published agent benchmark separates the same two things:
SWE-bench, Aider's leaderboard and the `evalplus` reports all commit per-instance JSON logs and
regenerate their tables and plots from them with a script; the raw logs are the artefact of
record, the figure is derived. LangSmith and Weights & Biases dashboards are the interactive
version of the same idea: the trace store is the truth, the chart is a query over it. Storing
per-node usage inside each result (rather than only in a separate ledger) mirrors how those logs
carry the token counts alongside the verdict, so one file is enough to reproduce a cost figure.

**Check it.**
```bash
uv run pytest tests/test_figures.py -q          # curve, averages, ledger fallback, three PNGs
uv run python evals/figures.py                  # regenerate docs/figures/*.png from evals/results/
uv run python evals/figures.py --no-ledger      # results-file data only; old files lose the cost profile
```
Seven tests run on a hand-built five-task suite: the curve equals `[0.2, 0.6, 0.6, 0.6]` and
ends at the pass rate, per-node averages divide by the number of runs with usage, the ledger
fallback picks the most recent run for a task id, `latest_results` keeps one file per label and
drops the self-checks, and old results files without `usage` still load. The rendered figures
were checked by eye on the real results, first with six quota errors (drawn as a pale stack, not
as failures) and then after `--rerun-errors` completed the run at 12/12. The cost profile puts
about 97% of the budget in `act` (about 29k tokens per run against under 1k for `plan`), which
is the number retrieval (milestone 5) is supposed to move.

---

## Step 5.1 — Repo loader and language-aware chunking

**What we built.** The first half of retrieval: turning a repository into a list of chunks that
are worth embedding. Two modules under `src/coder_agent/rag/`.

- `loader.py` walks the repo the way `git ls-files` would see it. It reads every `.gitignore`
  (root, nested, and `.git/info/exclude`), prunes ignored directories before listing them, always
  skips `.git`, `node_modules`, virtualenvs and our own `.coder-agent/` state, drops binaries
  (NUL byte in the first 8 kB, git's own test) and anything over `CODER_INDEX_MAX_FILE_KB`.
  Each file comes back as a `RepoFile`: POSIX-relative path, text with line endings normalised,
  a SHA-256 of the raw bytes, and a language guessed from the extension.
- `chunker.py` splits a file into `Chunk`s. For the eleven languages with a grammar table
  (Python, JavaScript, TypeScript/TSX, Go, Rust, Java, Ruby, C, C++, C#) it parses with
  tree-sitter and emits one chunk per top-level definition, with imports and constants between
  them grouped as `module` chunks. A class that is too big becomes a header chunk plus one chunk
  per method named `Class.method`; a single function that is too big is cut into overlapping line
  windows that keep its name. Everything else (Markdown, YAML, unknown extensions) is windowed by
  lines with both a line cap and a character cap. Every chunk carries `path`, 1-based
  `start_line`/`end_line`, `kind`, `symbol` and a content hash `id`, so a hit can be shown as
  `src/app.py:42-67 (Foo.method)`.
- `pyproject.toml` gains `pathspec` (gitignore matching) and `tree-sitter` with
  `tree-sitter-language-pack` (prebuilt grammars, wheels for Windows, no compiler needed).
- Four settings: `index_max_file_kb`, `chunk_max_chars`, `chunk_window_lines`,
  `chunk_overlap_lines`.

**Key concepts.**
- *Reuse git's judgement.* Deciding what is "source" is a hard, repo-specific question, and
  every repo has already answered it in `.gitignore`. Nested files matter: a pattern in
  `src/.gitignore` applies to `src/` only, `/build` at the root does not touch `src/build`, and a
  deeper `!keep.log` can re-include what the root ignored. The loader evaluates the chain of
  `.gitignore` files from the root down to the file's directory and lets the last one with an
  opinion win, which is exactly git's rule.
- *Prune, do not filter.* `os.walk(topdown=True)` lets you edit the directory list in place. An
  ignored `node_modules` is never entered, so a JavaScript repo with forty thousand dependency
  files indexes as fast as an empty one. Filtering after the walk would visit every one of them.
- *Chunk at syntax boundaries.* An embedding is one vector for one piece of text. A window that
  straddles the end of one function and the start of another encodes two topics at once and
  matches neither query well. Tree-sitter gives a concrete syntax tree in a few milliseconds for
  any file size, so the natural unit (a function, a class, the imports block) is cheap to find.
  Named chunks also give the retriever something to print: `graph/nodes.py:29-57 (make_plan_node)`
  is a citation the agent can open with `read_file` at exactly the right lines.
- *Two caps on a window.* Sixty lines of Python is roughly one chunk; sixty lines of Markdown is
  four. Windows stop at whichever limit comes first, and a single over-long line still gets its
  own window so nothing is silently skipped. The first version only capped lines and produced
  6 kB chunks from the README; the test that caught it is
  `test_windows_respect_the_character_budget_for_prose`.
- *Hash on the way in.* Step 5.2 builds an incremental index. Its unit of work is "this file's
  hash changed", so the loader computes SHA-256 once and stores it in `RepoFile`; the chunk `id`
  is a hash of path, line range and text, stable across runs for unchanged code.
- *Graceful degradation.* No grammar for the language, a grammar that recognises nothing, an
  undecodable byte: each falls back to a coarser strategy rather than dropping the file. A
  retriever that cannot see a file is worse than one that sees it in slightly awkward pieces.

**How the real tools do it.** Cursor, Sourcegraph Cody and Aider all chunk by syntax rather than
by fixed windows: Cursor's indexer and Cody's context engine parse with tree-sitter and embed
functions and classes, and Aider's repo map is built from tree-sitter tags (the same
`tags.scm` queries the grammars ship with) to list the definitions in each file. All of them read
`.gitignore` and add their own ignore file on top (`.cursorignore`, `.aiderignore`); our
`ALWAYS_IGNORED_DIRS` plays that role in miniature. Continue.dev and the LangChain
`RecursiveCharacterTextSplitter.from_language` take a cheaper route, splitting on language-specific
separator strings like `\ndef ` and `\nclass `; that works for well-formatted files and breaks
on nested definitions, which is why we pay for a real parser.

**Check it.**
```bash
uv run pytest tests/test_rag_loader.py tests/test_chunker.py -q     # 37 tests
uv run python -c "from pathlib import Path; from coder_agent.rag import load_repo, chunk_repo; f = load_repo(Path('.')); c = chunk_repo(f); print(len(f), 'files', len(c), 'chunks', max(len(x.text) for x in c), 'max chars')"
uv run python -c "from pathlib import Path; from coder_agent.rag import load_file, chunk_file; [print(c.kind.ljust(9), c.location) for c in chunk_file(load_file(Path('.'), Path('src/coder_agent/graph/nodes.py')))]"
```
The loader tests build a small repo in a temp dir with root and nested `.gitignore` files,
negations, an anchored pattern, `.git/info/exclude`, a PNG and an oversized file, and assert on
exactly which paths come out. The chunker tests cover Python (decorators travel with their
function, oversized classes split into `Foo.method` chunks), JavaScript (`export` kept, arrow
functions named, plain constants left as filler), Go, Rust (attributes attached, `impl` methods
split), the window arithmetic, empty files, and that every chunk's text equals the file's lines
`start..end`. On this repository the loader finds 321 files and the chunker produces about a
thousand chunks, none over the 1,500-character budget; `graph/nodes.py` comes out as one module
chunk and seven named functions.

## Step 5.2 — Incremental Chroma index and `coder index`

**What we built.** The second half of retrieval: chunks become vectors, vectors are persisted, and
re-running the indexer after an edit costs seconds instead of minutes. Two new modules under
`src/coder_agent/rag/` and one CLI command.

- `embeddings.py` defines an `Embedder` protocol (`embed_documents`, `embed_query`) and two
  implementations. `FastEmbedder` wraps fastembed, an ONNX Runtime port of the sentence-transformer
  models that needs no torch install; the default model is `snowflake/snowflake-arctic-embed-xs`
  (22M parameters, 384 dimensions, 512-token window), downloaded once into
  `~/.coder-agent/models`. `HashEmbedder` is a bag-of-words
  hashed into 64 buckets: no model, deterministic, and enough for tests to check that the right
  chunks come back.
- `index.py` holds `RepoIndex`, one Chroma collection per repository stored under
  `.coder-agent/chroma` inside that repository. `update()` loads the repo, diffs it against the
  index, deletes the chunks of changed and removed files, chunks and embeds the added and changed
  ones in batches of 256, and returns an `IndexStats` with the counts. `search(query, k)` runs a
  cosine nearest-neighbour query and returns `Hit`s carrying the original `Chunk` and a score in
  `[0, 1]`. A `progress(phase, done, total)` callback is called for the `chunk`, `embed` and
  `write` phases so the CLI can draw bars without the index knowing about Rich.
- `coder index <repo> [--rebuild] [--query TEXT]` in `cli.py` drives it with three Rich progress
  bars and prints the stats line; `--query` shows the top hits as a table so retrieval can be
  eyeballed before it is wired into the graph.
- `pyproject.toml` gains `chromadb` and `fastembed`. Three settings: `embedding_model`,
  `embed_batch_size`, `index_collection`, plus `models_dir` for the model cache.

**Key concepts.**
- *The index is its own manifest.* Every chunk is stored with the SHA-256 of the file it came
  from. The set of `(path, sha256)` pairs in the collection is therefore exactly the list of what
  has been indexed, and an update is three set differences against the files on disk: added,
  changed, removed. A separate manifest file would be a second source of truth that can drift
  from the vectors after a crash mid-write; reading the metadata back costs one `get()`.
- *Files, not chunks, are the unit of change.* When a file changes, all of its chunks are deleted
  and re-embedded. Diffing at chunk level would save embedding work in theory, but a chunk's `id`
  includes its line range, so one inserted line near the top invalidates every chunk below it
  anyway. Simpler bookkeeping wins; the cost is bounded by the size of one file.
- *Embed a header, store the body.* The text sent to the model is `path symbol\ncode`, so a query
  like "where are run tokens written to sqlite" can match `telemetry/ledger.py` through its path
  even when the body never says "ledger". The document stored in Chroma stays the raw code, so
  what the agent later reads is exactly what is in the file.
- *Cosine space.* The collection is created with `hnsw:space=cosine`. bge vectors are normalised,
  so cosine similarity is the intended metric, and `1 - distance` lands in `[0, 1]` which reads
  well in a table and fuses cleanly with a BM25 rank in the next step.
- *Lazy everything.* The Chroma client, the collection and the embedding model are created on
  first use. Importing the package, or constructing a `RepoIndex` to ask `exists()`, never pays the
  model load time, and the tests inject `HashEmbedder` so the suite does not download anything.
- *Asymmetric models want a query instruction.* arctic-embed and bge were trained with the
  sentence "Represent this sentence for searching relevant passages: " in front of every query
  and nothing in front of documents. fastembed does not add it. Without it, "where are run
  tokens written to sqlite" ranked five Markdown windows above `telemetry/ledger.py`; with it the
  ledger module is first. `embedding_query_prefix` carries the string, and `FastEmbedder` adds it
  in `embed_query` only.
- *Measure the embedder before trusting it.* The first default was `BAAI/bge-small-en-v1.5`, the
  usual recommendation for small English retrieval. fastembed serves it as an int8-quantised ONNX
  graph, and on this laptop's CPU (no AVX-512 VNNI, so the int8 kernels fall back to a slow path)
  it embedded 0.7 chunks per second: 28 minutes for this repository. The float32
  `snowflake/snowflake-arctic-embed-xs`, a model of the same size and retrieval quality, does 10
  chunks per second on the same chunks, and `all-MiniLM-L6-v2` does 39 but truncates at 256
  tokens, too short for our 1,500-character chunks. Quantisation is a speed-up only on hardware
  that has the instructions for it; a benchmark on 48 real chunks settled it in two minutes.
- *Windows detail.* `clear()` deletes the collection through the client rather than removing the
  directory: Chroma keeps its segment files memory-mapped while the process lives, and Windows
  refuses to unlink an open file.

**How the real tools do it.** Cursor computes a Merkle tree of file hashes for the workspace and
sends only the changed subtrees to its indexing service, which is the same "hash per file, diff
against what is stored" idea with a tree on top so a large repo can find its changed files in
logarithmic time. Sourcegraph Cody's local context engine and Continue.dev both keep a per-file
hash table next to their embeddings (Continue in SQLite, with the vectors in LanceDB) and
re-embed only the files whose hash moved. Aider skips vectors entirely and rebuilds its
tree-sitter repo map from a cache keyed by file mtime. On the model side, code-specialised
embedders (Voyage `voyage-code-3`, OpenAI `text-embedding-3`) beat bge-small by a clear margin,
but they are paid APIs; bge-small on the CPU is the best free option that runs on a laptop
without a GPU, and Step 7 measures a reranker on top of it.

**Check it.**
```bash
uv run pytest tests/test_index.py -q           # 12 tests, HashEmbedder, about 10 seconds
uv run coder index . --rebuild                  # first build: downloads the model, embeds everything
uv run coder index . -q "where are run tokens written to sqlite"   # second run: all files unchanged
```
The tests build a three-file repo in a temp dir and assert that the first update embeds every
file, the second embeds nothing, editing one file re-embeds only that file, deleting a file drops
its chunks, a new file is added without touching the rest, the index survives a new `RepoIndex`
instance, `clear()` forces a full rebuild, search returns the chunk with its `path:lines (symbol)`
location, and the progress callback sees all three phases.

## Step 5.3 — Hybrid retriever: BM25 + dense with reciprocal rank fusion

**What we built.** `src/coder_agent/rag/retriever.py`: a lexical index, a fusion function and a
retriever that can run either side alone or both together. `coder index -q "..." --mode
hybrid|dense|bm25` shows what each mode returns on a real repository.

- `tokenize()` turns text into search terms the way code is actually written: it emits every
  identifier whole (`write_ledger`) and also its snake_case and camelCase parts (`write`,
  `ledger`; `HTTPServer` gives `http`, `server`), lower-cased, minus a short stopword list of
  English glue and Python keywords.
- `BM25` is a forty-line Okapi BM25 over an in-memory corpus of `(chunk_id, text)`: term
  frequencies per document, document frequencies, average length, and the standard scoring
  formula with `k1=1.5`, `b=0.75`. It indexes the same `path symbol\ncode` text the embedder saw,
  so file names and function names are searchable lexically too.
- `reciprocal_rank_fusion()` merges any number of rankings: each document scores the sum of
  `1 / (60 + rank)` over the lists it appears in.
- `HybridRetriever(index, mode)` wraps a `RepoIndex`. In `dense` mode it is `index.search`; in
  `bm25` mode it builds the lexical index from `index.all_chunks()` on first use and caches it; in
  `hybrid` mode it asks both sides for `retrieval_candidates` (20) hits, fuses, and returns the
  top `k`. `invalidate()` drops the cache after an index update.
- Two settings: `retrieval_mode` (the knob the Step 5.5 ablation turns) and
  `retrieval_candidates`.

**Key concepts.**
- *Two retrievers because there are two kinds of query.* "Where is `write_ledger` called" names
  a token that appears verbatim in the code; a lexical index finds every occurrence in one pass,
  while an embedding model sees an odd rare word and often ranks a semantically similar function
  above the exact match. "Where do we record how many tokens a run used" contains no identifier
  from the code at all; only the embedding knows that `tokens_in` and `ledger` are about it.
  Published numbers on code search (CodeSearchNet, CoIR) show hybrid beating either alone by a
  few points of recall, and the failure cases of each side are visible in `--mode bm25` versus
  `--mode dense` on this repo.
- *Fuse ranks, not scores.* A BM25 score of 12.3 and a cosine similarity of 0.81 are on
  unrelated scales, and any weighting between them would need re-tuning per corpus. RRF
  (Cormack, Clarke and Büttcher, 2009) only asks "at what position did each list put this
  document", which is scale-free. The constant 60 flattens the curve so that first place is
  worth only slightly more than fifth; the effect is that a document both retrievers agree on
  outranks one that a single retriever is very confident about. The test
  `test_rrf_prefers_documents_present_in_both_rankings` pins that property.
- *Ask for more than you return.* Each side contributes 20 candidates when the caller wants 8.
  A chunk ranked sixth by both lists should beat one ranked first by one list and absent from
  the other; that can only happen if the sixth-ranked entries are in the pool.
- *Own the small algorithm.* `rank-bm25` on PyPI would have done the scoring, but its tokenizer
  is `str.split()`. The whole point here is that `read_file`, `readFile`, `read` and `file`
  should all find the same chunk, and the split is where that decision lives. Forty lines of
  code and a formula every retrieval textbook prints is cheaper than a dependency whose one
  tunable part we would have to bypass.
- *The index is the corpus.* BM25 is rebuilt from the Chroma collection rather than persisted
  next to it. One repository is a few thousand chunks, the build takes tens of milliseconds, and
  a second on-disk structure would need its own change tracking to stay in step with the vectors.
  This trades a little start-up time for zero consistency bugs.
- *The BM25 idf variant.* `log(1 + (N - df + 0.5) / (df + 0.5))` rather than the original
  `log((N - df + 0.5) / (df + 0.5))`: the `1 +` keeps idf positive when a term appears in more
  than half the documents, which `self` or `return` would otherwise do in a Python repo and score
  negative. Lucene made the same change in 2016.

**How the real tools do it.** Every production code assistant runs a hybrid. GitHub Copilot's
workspace retrieval combines a local lexical index (a trigram or full-text engine on the client)
with embeddings computed server-side and merges them before reranking. Sourcegraph Cody
queries Zoekt, the trigram search engine behind sourcegraph.com, alongside its embeddings and
fuses by rank. Cursor's `@codebase` is dense-first with a reranker, but the plain `@` symbol
lookup is a lexical index. Continue.dev's codebase retrieval is exactly this design: SQLite FTS5
for the lexical side, LanceDB for vectors, RRF to merge, an optional reranker after. Elastic and
OpenSearch both ship RRF as a built-in query type for the same reason we picked it: it is the
fusion that needs no tuning when the two scores are not comparable.

**Check it.**
```bash
uv run pytest tests/test_retriever.py -q                                    # 14 tests
uv run coder index . -q "write_ledger" --mode bm25                           # exact identifier
uv run coder index . -q "where are run tokens recorded" --mode dense         # no identifier in query
uv run coder index . -q "where are run tokens recorded" --mode hybrid        # fused
```
The tests cover the tokenizer (whole identifier and its parts, stopwords, case), BM25 (the
defining chunk ranks first for its identifier, unknown terms return nothing, scores are positive
and sorted, idf stays positive for a term in every document), RRF (a document in both lists beats
one in a single list; a single list keeps its order), and the retriever over a real Chroma index
with the hash embedder: each mode returns the expected chunk, the hybrid score is the RRF sum,
the mode can be overridden per call, an invalid mode is rejected, and `invalidate()` is what
makes a newly indexed file searchable.

## Step 5.4 — `retrieve_context` node and the retrieval eval

**What we built.** Retrieval joins the graph, and gets its own benchmark that runs without a
single model call.

- `graph/retrieval.py` adds a `retrieve_context` node between `prepare` and `plan`. It brings the
  repository index up to date (`RepoIndex.update()`, incremental, so a repo the agent already
  worked on costs a hash comparison), searches it with the task text in the configured mode, and
  writes two things to state: `context`, the top `retrieval_k` (6) chunks rendered as fenced
  blocks headed `path:lines (symbol)` within a `retrieval_context_chars` (6,000) budget, and
  `retrieved`, the list of locations for the UI and the ledger. `retrieval_mode=off` makes the
  node a no-op, which is the ablation's baseline.
- `build_graph(..., retriever=...)` takes the retriever as a parameter. The default in
  `agent.run_agent` is the real one; tests pass a fake or nothing. The plan prompt gains the
  context as part of the human turn; the act system prompt gains it as a section that tells the
  model it is a hint and to read before editing.
- `evals/retrieval.py` and `evals/retrieval_eval.py` measure retrieval on the benchmark tasks.
  The gold files of a task are the files the reference solution changes that already exist in
  the starting repo. For every task the repo is indexed (in a temp dir, one shared embedder), the
  task prompt is the query, and the distinct files in hit order are scored: recall@k over the
  gold files and MRR (1 / rank of the first gold file), per mode. Results go to
  `evals/results/<stamp>-retrieval.json` with a Markdown table.
- The default embedding model changes to `snowflake/snowflake-arctic-embed-xs`; see the note
  added to Step 5.2.

**Key concepts.**
- *Retrieval is a node, not a tool.* The model could be given a `search_index` tool and left to
  call it. Making retrieval a node that runs before planning means the planner never starts
  blind, the cost is one query per run instead of one per whim, and the effect can be switched
  off from a setting for a controlled comparison. Production agents do both: a retrieval pass up
  front and a search tool for follow-ups; the tool half is `search_code`, which we already have.
- *Run it once.* After failing tests the graph goes `reflect -> plan`, not through
  `retrieve_context` again. The task has not changed, so the query has not changed; what is new
  is the agent's own edits, and those are already in the conversation as tool calls. Re-running
  would re-embed the changed files for nothing. `test_retrieval_runs_once_even_when_the_loop_iterates`
  pins this.
- *Injection through the factory.* `build_graph` takes `retriever: Callable[[Path, str],
  list[Hit]] | None`. Nothing in the graph imports Chroma or the embedding model; that lives in
  `repo_retriever`, imported lazily. The whole graph test-suite keeps running with a scripted LLM
  and no vector store, and the node is tested with a two-line fake.
- *A budget in characters, not chunks.* Six chunks of a generated file can be 20 kB. The formatter
  adds hits in rank order until the budget is spent and truncates a single oversized first hit
  rather than returning nothing, so the planner always sees the best match.
- *Measure retrieval on its own.* The agent benchmark costs a day of API quota and is noisy. The
  retrieval eval runs in about a minute on the CPU and is deterministic, so a change to the
  tokenizer or the fusion constant can be checked before it is trusted. The gold set comes for
  free from `solution/`: the files a fix touches are by definition the ones retrieval should
  surface.
- *What the numbers say.* On all 42 tasks (40 with a gold file; the two add-test tasks only
  create new files, which cannot be retrieved):

  | mode | tasks | recall@1 | recall@3 | recall@5 | MRR |
  |---|---|---|---|---|---|
  | hybrid | 40 | 0.08 | 0.99 | 1.00 | 0.56 |
  | dense | 40 | 0.08 | 0.99 | 1.00 | 0.56 |
  | bm25 | 40 | 0.08 | 0.99 | 1.00 | 0.55 |

  Two lessons. First, in 34 of 40 tasks the file ranked first is the visible test file, not the
  source: the prompt names the function, and the test file repeats that name more often than the
  module that defines it. The source is almost always second, which is why recall@3 is perfect
  and recall@1 is not. With `retrieval_k=6` chunks the planner sees both, so this costs nothing
  today, but on a large repository it argues for down-weighting `tests/` at query time. Second,
  the benchmark repos have three to five files, so file-level recall saturates at k=3 and the
  three modes cannot be told apart here. The eval is the right instrument, the corpus is too
  small. Step 5.5 asks the question that matters,
  pass rate with and without retrieval, and a larger-repo retrieval set is a natural follow-up.

**How the real tools do it.** Cursor and Copilot both run a retrieval pass before the first
model call and inject the results into the prompt, then let the model search further with tools;
Copilot's "workspace" agent calls this the context-gathering phase. Aider injects its repo map
(the tree-sitter definitions ranked by PageRank over the import graph) into every prompt, a
retrieval step that is structural rather than by similarity. On measurement, SWE-bench papers
report exactly our metric: Agentless and AutoCodeRover evaluate "file localisation" as recall of
the files touched by the gold patch at k = 1, 3, 5 before they measure whether the patch passes,
because a wrong file ends the attempt. Our eval is that step on our own tasks.

**Check it.**
```bash
uv run pytest tests/test_retrieval_node.py tests/test_retrieval_eval.py -q   # 14 tests
uv run python evals/retrieval_eval.py --suite all --k 1 --k 3 --k 5          # table above, about a minute
uv run coder run evals/suite/fix-bug-duration-units/repo "fix the duration parsing" -v
```
The run prints a dim `Context · 6 chunks: ...` line before the plan panel, listing what the
planner was shown. The node tests check the formatting and its budget, that the planner and the
actor both receive the context, that a graph built without a retriever adds no section, and that
retrieval runs once across two iterations. The eval tests build a task directory with one
changed, one unchanged and one new solution file, and check the gold set, the metrics, the
Markdown table, the JSON round trip, and an end-to-end run over a real index in all three modes.

## Step 6.1 — SQLite checkpointer and `coder run --resume`

**What we built.** Every run now leaves a trail it can be picked up from.

- `agent.run_agent` compiles the graph with LangGraph's `AsyncSqliteSaver` pointed at
  `<repo>/.coder-agent/checkpoints.sqlite`, next to the vector index. Each run gets a thread id
  (eight hex characters, or one you pass in), shown in the header panel and in the footer, and
  stored in the ledger's tags so a run in `coder stats` can be matched to its checkpoints.
- `agent.resume_agent(repo, thread_id)` opens the same file, refuses an unknown thread
  (`UnknownThread`), returns a finished thread without running anything, and otherwise streams
  the graph again with `None` as input: LangGraph takes the saved state and executes the nodes
  that were scheduled next. The ledger gets one record covering the whole thread, tagged
  `resumed: true`.
- `coder run <repo> --resume <thread>` is the CLI face of that. The task argument becomes
  optional (one of task or `--resume` is required). When a run dies with any exception the CLI
  prints the exact command to continue it, rather than a stack trace and a lost afternoon.
- `tests/fakes.ScriptedLLM` gains `fail_on_call={n}`: call *n* raises and the scripted reply is
  kept for the retry, which is what a 429 followed by a resume looks like from the graph's side.

**Key concepts.**
- *Checkpoints are per superstep.* LangGraph writes the state after every node finishes, not
  during. If `act` dies on a rate limit, the checkpoint holds the state up to the end of the
  previous node, with `next == ("act",)`. Resuming re-runs `act` from the same messages: the
  failed model call was never committed, so `steps` is not bumped for it and the conversation
  has no half-written turn. `test_crash_then_resume_continues_from_the_failed_node` pins exactly
  this: four model calls in total (plan, the act that failed, the act that retried, the final
  answer), one plan, and the retried act prompted with the plan from the checkpoint.
- *The thread id is the memory key.* A checkpointer stores many checkpoints per thread and many
  threads per file; `configurable.thread_id` in the run config chooses which conversation you are
  continuing. `coder chat` (step 6.3) will reuse the same id across turns; here one run is one
  thread. Everything about resume is a consequence of that one config key.
- *Input `None` means "continue".* Passing the original input again would re-apply it through
  the reducers (`add_messages` would append the task a second time). Passing `None` tells
  LangGraph there is nothing to merge: start from the checkpoint and run `next`.
- *Why the file lives in the repo, not in `~/.coder-agent`.* Checkpoints hold tool outputs and
  diffs of that repository; they belong with it and disappear with it. The eval runner deletes
  each task's working copy, checkpoints included, so 42 benchmark runs leave nothing behind. The
  ledger stays in the home directory because it aggregates across repos.
- *Messages survive the round trip.* The saver serialises state with LangGraph's
  `JsonPlusSerializer`, which knows LangChain message types, so `ToolMessage`s with their
  `tool_call_id`s come back as the same objects and the model sees an intact tool-call history.
  The MCP tools are reloaded on resume; a tool result already in the messages is never re-run.
- *One ledger record per thread.* The crashed attempt writes nothing (the exception leaves
  `_drive` before the record). The resumed attempt seeds its fold with the checkpoint's usage
  and outcome fields, so the record it writes counts every token the thread spent, including the
  ones before the crash. Wall time is only the resumed part; it is the number that could not be
  reconstructed.

**How the real tools do it.** Claude Code's `--resume` and `--continue` reopen a session from a
transcript stored per project directory; Codex CLI and Aider keep a per-repo history file for
the same purpose. Devin and OpenHands go further and store the whole event stream so a run can be
replayed and forked from any point, which is what LangGraph's checkpoint history
(`graph.get_state_history`) enables and what human-in-the-loop (step 6.2) will build on: an
`interrupt()` is just a checkpoint whose `next` is the node waiting for an answer. Cursor's
agent "checkpoints" are the same idea applied to the working tree: a restore point per step.
The common lesson is that durable execution is a property of the orchestrator, not of the model;
LangGraph ships it as a pluggable saver (SQLite here, Postgres in production) so the graph code
does not change.

**Check it.**
```bash
uv run pytest tests/test_resume.py -q                # 7 tests, no network
uv run coder run evals/suite/fix-bug-duration-units/repo "fix the duration parsing"
# note the thread id in the header; press Ctrl+C mid-run, then:
uv run coder run evals/suite/fix-bug-duration-units/repo --resume <thread>
uv run coder run evals/suite/fix-bug-duration-units/repo --resume nope      # exit 2, unknown thread
```
Resuming a run that already finished prints its status and runs nothing. The checkpoint file is
`.coder-agent/checkpoints.sqlite` inside the target repo; `sqlite3` on it shows one row per
node per thread in `checkpoints`.

## Step 6.2 — Human-in-the-loop: approve edits and commands before they run

**What we built.** The agent now asks before it touches anything.

- `graph/approval.py` defines `RISKY_TOOLS` (`edit_file`, `write_file`, `run_command`) and an
  `approve` node. The node calls LangGraph's `interrupt()` with the pending risky calls; the run
  stops, the checkpoint is written, and the stream ends with an `__interrupt__` event. The
  answer comes back through `Command(resume=...)`: `True` lets `ToolNode` run the calls
  unchanged; `False` or a string answers every tool call on the model's message with a
  `ToolMessage` saying the user declined (and why), and routes back to `act`.
- `build_graph(..., require_approval=True)` wires the node in. The routing after `act` only
  detours through `approve` when the last message carries at least one risky call; reads and
  searches go straight to `tools`. Without the flag the graph is unchanged, which is how the
  eval runner keeps running unattended.
- `agent.run_agent(..., approve=hook)`: the hook receives the payload and returns the decision.
  `_drive` loops: stream until the graph ends or interrupts, ask, restart the stream with the
  answer. `approve=None` means nobody is watching, so the gate is not built, and a resumed
  thread that was parked at a prompt is waved through.
- `coder run` asks by default: each pending call is shown as a unified diff (`edit_file`), the
  file content (`write_file`) or the command line, followed by `Approve? y / n / a reason`. A
  reason is passed to the model verbatim, which turns the prompt into a steering channel. `--yes`
  (`-y`) skips it all. The act system prompt tells the model a rejection can happen and not to
  retry the same call.

**Key concepts.**
- *`interrupt()` is a checkpoint with a question attached.* Nothing about it is special: the
  node raises, LangGraph saves the state with `next == ("approve",)`, and the caller sees the
  payload. That is why Ctrl+C at the prompt is harmless, and why `--resume` on such a thread asks
  the same question again (`test_interrupted_at_the_prompt_then_resumed_asks_again`). It is also
  why the gate needs a checkpointer and came right after step 6.1.
- *The node re-runs from the top.* On resume LangGraph does not continue from the line after
  `interrupt()`; it executes the node again and `interrupt()` returns the answer instead of
  raising. Anything before the call runs twice, so the node computes its payload from state and
  does nothing else. Side effects belong after the call, or in the next node.
- *A rejection has to be a tool result.* The model's message says "call `edit_file`"; the
  provider will refuse the next turn unless every call id gets a `ToolMessage`. So a rejection
  answers all calls on that message, including the safe ones that were never executed, and the
  text carries the user's reason. `test_rejection_answers_every_call_and_returns_to_act` checks
  both ids are answered and that the model's next prompt contains the rejection.
- *Gate at the graph, not in the tool.* The MCP server could ask for confirmation itself, but it
  runs in a subprocess with no terminal, and the eval runner has no human. Putting the gate in
  the orchestration layer keeps the tools dumb and the policy in one place, and lets a
  different front end (`coder chat`, a web UI) answer the same interrupt differently.
- *Approve per message, decide per call later.* The payload lists the risky calls; today the
  answer applies to all of them. The shape is ready for per-call answers (a dict of id to
  decision) without changing the graph, which is the obvious next refinement.

**How the real tools do it.** Claude Code asks before every file edit and shell command unless
the user has allowed the tool or the pattern (`--dangerously-skip-permissions` is the equivalent
of `--yes`), and treats a typed reply to the prompt as feedback to the model, exactly as the
reason string here. Aider auto-applies edits but asks before running commands and lets you
`/undo`. Cursor's agent shows the diff and waits for Accept. OpenHands has a "confirmation mode"
that pauses the event stream before each action. All of them separate *reads*, which are free,
from *writes and execution*, which are gated; the `RISKY_TOOLS` set is that line drawn for our
six tools.

**Check it.**
```bash
uv run pytest tests/test_approval.py -q                      # 12 tests, no network
uv run coder run evals/suite/fix-bug-duration-units/repo "fix the duration parsing"
# answer y to the edit, then type "also add a test for negative values" to the next one
uv run coder run evals/suite/fix-bug-duration-units/repo "fix the duration parsing" --yes
```
The first prompt shows a coloured diff and waits. A reason rejects the edit; the next model turn
starts from that feedback. With `--yes` the run looks exactly like it did in step 3.

## Step 6.3 — `coder chat`: many tasks, one thread

**What we built.** A conversation with the agent, where every message is a full plan/act/test
run and the next message remembers the last.

- `agent.run_agent` treats a `thread_id` that already finished a run as a follow-up turn. It
  streams the graph with a *turn patch* instead of fresh inputs: the new task, a `HumanMessage`
  carrying it, `turn + 1`, and the loop and verdict fields reset (`iteration`, `steps`,
  `status`, `tests_passed`, `test_output`, `plan`, `summary`). `messages` is not reset: its
  reducer appends, so the earlier turns stay in the conversation the model sees. A thread whose
  last run has not finished raises `ThreadInProgress`, because a new task on top of a half-done
  one would be a mess; `resume_agent` is the way to finish it.
- `state.turn` is new: 1 for a `coder run`, growing per chat message. The ledger record of a
  follow-up turn is tagged with it and counts only that turn's tokens, while the graph state's
  `usage` keeps the thread total.
- `coder chat <repo> [--thread ID] [-y]` is a loop around that: print the thread id, read a
  line, run it, print the footer, repeat. `/resume` continues an interrupted turn, `/quit`
  leaves, Ctrl+C prints how to come back. A turn that dies keeps the session alive; the
  checkpoint is on disk either way.

**Key concepts.**
- *A chat is a thread with more than one input.* Nothing in the graph knows about chat. The
  same `StateGraph` runs from `START` each time; what differs is that the second invocation on
  the same `thread_id` starts from the checkpointed state instead of an empty one. Reducers
  decide what a re-entry means: `add_messages` appends, plain fields are overwritten by the
  patch, untouched fields keep their value (that is how `test_command`, detected once by
  `prepare`, carries over).
- *Reset what must not carry over.* The bug that this design invites is inheriting counters:
  turn two would start with `iteration == 1` from turn one and, after one failing test run, hit
  `max_iterations` early. `_new_turn` is the explicit list of fields that describe *a run*
  rather than *a thread*. `test_second_task_on_a_finished_thread_keeps_the_history` checks both
  halves: the model's second act call sees `first task, done one, second task`, and the second
  turn's counters start at zero.
- *Retrieval runs again, planning runs again.* Each turn passes through `retrieve_context` and
  `plan` with the new task, so the planner is briefed for the new request, and the old plan is
  cleared rather than shown next to it. The conversation, not the plan, is the memory.
- *Dispatch is testable without a model.* The chat loop is tested by replacing `run_agent` and
  `resume_agent` with fakes and feeding lines on stdin, which pins the commands and the error
  path in milliseconds. The graph semantics are tested separately with the scripted model. Two
  small tests instead of one slow one.

**How the real tools do it.** Every interactive coding agent is this loop: Claude Code, Aider
and Codex CLI keep one growing conversation per session and run the tool loop per message, and
all three persist the transcript so a session can be reopened. The difference is what they
reset: Aider re-sends its repo map every turn (our `retrieve_context`), Claude Code compacts the
transcript when it grows (our step 3.3 `manage_context`, which now also runs across turns), and
none of them carry a previous turn's retry budget forward. LangGraph's own docs model chat as
exactly this, a thread id on a checkpointed graph, which is why the whole step is under a
hundred lines.

**Check it.**
```bash
uv run pytest tests/test_chat.py -q                                  # 5 tests, no network
uv run coder chat evals/suite/fix-bug-duration-units/repo
# you> fix the duration parsing
# you> now add a test for negative durations
# you> /quit
uv run coder chat evals/suite/fix-bug-duration-units/repo --thread <id>   # picks the thread up
```
The second message's plan panel shows the new task while the model's edits reference what it
changed in the first; `coder stats` lists two runs tagged with the same thread, the second with
`turn: 2`.

## Step 6.4 — Docker sandbox: the model's commands run in a throwaway container

**What we built.** A second implementation of "run this command" and a switch between the two.

- `sandbox/docker.py` runs one command as `docker run --rm` on a fresh container: the target repo
  is mounted read-write at `/work` and is the only mount, `--network none`, `--memory 1g`,
  `--cpus 1`, `--pids-limit 256`, `--cap-drop ALL`, `--security-opt no-new-privileges`, a tmpfs on
  `/tmp`, and on Linux `--user <uid>:<gid>` so files the tests create are not owned by root.
  `build_argv` is a pure function so all of that is pinned by a test that never talks to a daemon.
- `sandbox/Dockerfile` is the default image (`coder-sandbox`): `python:3.11-slim` plus pytest.
  `ensure_image` builds it on first use; any other image name must already exist, because
  building an arbitrary tag from an unknown context is not something a sandbox should do by itself.
- `sandbox/__init__.py` is the dispatcher. `run_command(repo, command)` reads
  `settings.sandbox_mode` and calls `local` or `docker`; the MCP server's `run_command` tool and
  the graph's `run_tests` node both import it, so neither knows which one it got.
  `configure(mode)` applies `--sandbox`, probes the daemon and builds the image before the first
  model call, so a missing Docker costs zero tokens.
- The tool server is a separate process with its own `Settings`. `tools/client.server_config`
  now passes the parent's environment plus `CODER_SANDBOX_*` mirrored from the in-process
  settings, so `coder run --sandbox docker` reaches the child too.
- `--sandbox local|docker` on `coder run`, `coder chat` and `evals/run_evals.py`;
  `CODER_SANDBOX_MODE` in `.env` sets the default. The denylist still runs first in Docker mode,
  so `git push` gets the same `ERROR:` in both modes instead of a puzzling "could not resolve host".

**Key concepts.**
- *Two kinds of guardrail.* The denylist stops the model from doing something obviously
  destructive by accident; it can only block patterns it knows and it is trivially wrong for a
  command it has never seen. A container turns the question around: instead of listing what is
  forbidden, it lists what exists. Nothing outside `/work` is there to delete, there is no
  network to exfiltrate to, and a runaway process hits a memory cap instead of the host's. The
  four integration tests say exactly this: pytest runs against the mounted repo, a file written
  inside appears on the host, `ls /` shows no host directories, and opening a socket fails.
- *Timeouts need two kills.* `subprocess.run(timeout=)` kills the `docker` CLI, which is only a
  client; the container keeps running until the daemon notices the detached client, which it
  may never do. So every container gets a name and a timeout is followed by `docker kill <name>`.
  The unit test asserts the second call and that its name matches the first.
- *Infrastructure failures must not look like test failures.* When the daemon died mid-run during
  this step, the tool returned `exit_code=125` and a connection error as if the tests had failed;
  a model would happily start "fixing" that. `DockerUnavailable` is now raised for that shape
  (CLI exit 125 or 127, empty stdout, the daemon's own wording on stderr) and it subclasses
  `SandboxError`, so the tool server returns it as `ERROR: docker sandbox unavailable ...`; in the
  `run_tests` node it stops the run with a checkpoint, and `coder run --resume` continues once
  Docker is back.
- *Check the answer, not the exit code.* `docker info --format '{{.ServerVersion}}'` exits 0 with
  an empty string when the daemon is down: the client half of the report succeeded. The first
  version of the probe trusted the exit code and the integration tests ran against a dead daemon
  instead of skipping. The probe now requires a non-empty version.
- *Configuration has to cross a process boundary.* A CLI flag mutates the client's `settings`
  object; the MCP server loaded its own copy from `.env` at start-up and would happily keep
  running commands on the host. Passing `env` to the stdio transport fixes that, but a partial
  `env` replaces the child's whole environment (no PATH, no API keys), which is why the parent's
  environment is copied first and the overrides layered on top.
- *What it costs, and what broke while measuring.* The unit tests inject a fake `subprocess.run`
  and take under a second; the first real run, on Docker Desktop with the WSL2 backend, built the
  image (pull `python:3.11-slim`, install pytest) and ran the four container tests in under 30
  seconds inside the 50-test sandbox run. The full 5-minute suite then killed Docker Desktop: its
  WSL engine refused to restart with `OCI runtime create failed ... File exists` and needs a
  `wsl --shutdown`. So the per-command container overhead is not in this table yet; on the host
  `echo hi` takes 0.02 s and the one-test pytest 1.7 s, and a container start is typically a few
  hundred milliseconds to a couple of seconds on top of each command. The default stays `local`
  because the eval numbers were taken there and the free-tier budget is tokens, not seconds;
  `docker` is the mode for a repo you do not trust the model with. The crash did produce one
  useful test: with the daemon down, `coder run ... --sandbox docker` exits in about a second with
  `Sandbox: docker daemon not reachable` and no model call, and the unknown mode `firecracker`
  is rejected the same way.

**How the real tools do it.** This is the layer every serious agent has grown. OpenAI Codex runs
in a network-disabled container by default and needs an explicit flag to allow egress; Claude
Code's sandbox mode restricts filesystem writes to the project and proxies network access, and
its devcontainer reference setup uses a firewall allowlist; SWE-agent and OpenHands run every
command inside a per-task Docker container with the repo mounted, exactly this shape, because an
agent that runs `pip install` and arbitrary tests thousands of times a day will eventually do
something you did not want. The common pattern is the one here: the model never sees the
boundary, it just gets an exit code and output, and the harness decides where the process lives.

**Check it.**
```bash
uv run pytest tests/test_docker_sandbox.py -q     # 21 unit tests, +4 in containers when a daemon is up
uv run coder run evals/suite/fix-bug-duration-units/repo "fix the duration parsing" --yes --sandbox docker
```
The run prints a `Sandbox · docker · image=coder-sandbox network=none ...` line, and
`docker ps` during the test step shows a `coder-<id>` container that is gone afterwards. Stop
Docker Desktop and run the same command: it exits immediately with `Sandbox: docker daemon not
reachable`, before any model call.

## Step 6.5 — Retry, fallback, and knowing which model actually answered

**What we built.** The path from a node to the network now has three layers, and the ledger
records what each of them did.

- `llm.get_llm` builds: the primary model with the provider SDK's retries pinned to
  `llm_sdk_retries` (2); around it `ChatModelRetry`, a LangChain-level retry with exponential
  jitter for `llm_attempts` (2) tries on any provider error (`groq.APIError`,
  `GoogleAPICallError`); around that `with_fallbacks([...])` to the Gemini model. `ChatModelRetry`
  is `RunnableRetry` plus one `__getattr__`: the stock wrapper forwards `invoke` but not
  `bind_tools`, so the `act` node could not have used it.
- `telemetry.ProviderEvents` is a callback handler that counts `on_llm_error` by exception type.
  `agent._drive` passes it in the run config, so every model call in every node reports to it,
  and the count goes into a new ledger column `provider_errors_json` (added to existing ledgers
  by an `ALTER TABLE` migration on open).
- `RunRecord.fallback_calls` derives, from the per-node `models` counts the ledger already had,
  how many calls were answered by a model other than the configured primary. `coder stats` now
  prints a providers line; the run footer names the failed attempts and the fallback share.
- A run that dies is recorded too, with status `error` and the exception type in its tags, and
  the record rides on the exception so the CLI can print its footer. Without this, a failure
  left no trace in the ledger, and the fallback's own error was invisible: `with_fallbacks`
  re-raises only the primary's exception.

**Key concepts.**
- *Retries live at more than one level, and they multiply.* The Groq SDK already retries 429
  and 5xx with the server's `Retry-After`; Gemini's client defaults to six retries. A LangChain
  retry on top of that means a dead provider costs (SDK attempts) x (LangChain attempts) calls
  before the fallback gets a chance. Pinning both counts makes the worst case a number you can
  say out loud: at most 2 x 3 requests and about ten seconds, which is what the real run below
  took.
- *The SDK does not retry the errors that matter most here.* The 400 `tool_use_failed` seen in
  the ablation run is a 4xx, so the SDK treats it as the caller's fault and gives up, but the
  cause is the model emitting bad JSON, which is random. That is the case for a retry above the
  SDK: same model, same prompt, one more try, before spending a fallback request that is capped
  at 20 a day.
- *Wrapping a chat model is not free.* `Runnable.with_retry()` returns a generic binding;
  LangChain's `RunnableWithFallbacks` goes out of its way to forward chat-model methods and to
  apply them to the fallbacks too, but the retry wrapper does not. `ChatModelRetry.__getattr__`
  forwards the call and re-wraps any runnable it returns, and the `-> Runnable` annotation on
  the proxy is what the fallbacks wrapper inspects before mirroring the call onto the fallback.
  `test_bind_tools_survives_retry_and_fallback_wrapping` pins the whole sandwich.
- *Measure with callbacks, not with wrappers.* A handler in the run config reaches every model
  call in every node without any node knowing it is there. Counting `on_llm_error` per exception
  type is one line and answers the question the resume line needs: how often did the free tier
  fail, and did the fallback carry the run.
- *What the ledger said once it could.* Of the 17 runs recorded before this step, the fallback
  answered at least one call in 12, and 25 of 191 calls overall (13%) went to Gemini. Most of
  those runs show exactly one Gemini call, which fits a single `tool_use_failed` glitch per run
  being handed straight to the fallback; the retry layer should now keep those on the primary.
  That is a hypothesis the next eval run can check in `coder stats`.
- *Verified under a real rate limit.* Groq's 200k tokens-per-day window was exhausted when this
  step was finished, so a `coder run` on a copy of a benchmark task was the test:

  | what | value |
  |---|---|
  | wall time until the run gave up | 9.8 s |
  | Groq attempts on the failing call | 2 (`RateLimitError` x2) |
  | Gemini attempt | 1 (`GoogleRateLimitError`, its own daily quota was gone too) |
  | ledger row | status `error`, tag `error: RateLimitError`, both counts stored |

  Before this step the same situation produced a traceback and nothing in the ledger.

**How the real tools do it.** Every hosted agent has this stack. Claude Code and Cursor retry
transient API errors with backoff and surface the count in the UI; Aider retries with backoff
on rate limits and has a `--weak-model` for cheap side calls, which is a planned fallback rather
than an emergency one. LiteLLM, the router most open-source agents put in front of providers,
does exactly our three layers as configuration: `num_retries`, `fallbacks`, and per-model
success and failure counters. OpenAI's and Anthropic's SDKs both honour `Retry-After` and both
stop at 4xx, the same split that motivates the LangChain-level layer here.

**Check it.**
```bash
uv run pytest tests/test_provider.py -q            # 9 tests, no network
uv run coder stats                                 # "providers · fallback answered in N run(s) · failed attempts: ..."
uv run coder run evals/suite/fix-bug-duration-units/repo "fix the duration parsing" --yes
```
The footer of a run that needed the fallback reads like `... · 1 BadRequestError · 1 answered by
the fallback`. To see the retry layer alone, set `CODER_FALLBACK_MODEL=` (empty) in `.env` and
run during a quota outage: the run fails after `llm_attempts` tries and the ledger row shows
`{"RateLimitError": 2}` with status `error`.

## Step 7.3 — Reranker: a cross-encoder as an optional second retrieval stage

**What we built.** The retriever can now take a second, slower look at its own shortlist.

- `rag/reranker.py`: a `Reranker` protocol (`score(query, texts) -> list[float]`), an
  `OverlapReranker` stand-in for tests, and `FastEmbedReranker`, which wraps fastembed's
  `TextCrossEncoder` with `Xenova/ms-marco-MiniLM-L-6-v2` (80 MB, ONNX, CPU) cached in the same
  `~/.coder-agent/models` as the embedder. `default_reranker()` returns one when
  `CODER_RERANK=true`, else `None`.
- `HybridRetriever(..., reranker=, rerank_candidates=)`: when a reranker is present, the first
  stage (bm25, dense or hybrid) is asked for `rerank_candidates` chunks instead of `k`, the
  reranker scores each `(query, path-and-symbol header + code)` pair, and the top `k` by that
  score are returned with the cross-encoder logit as `Hit.score`. Ties keep the first-stage
  order; `search(..., rerank=False)` skips the stage per call.
- The `retrieve_context` node passes `default_reranker()`, so the agent gets the stage from one
  setting; `rerank_candidates` (20) and the model name are settings too.
- The retrieval eval understands `<mode>+rerank` modes and `evals/retrieval_eval.py --rerank`
  adds the reranked twin of every selected mode, so the table shows both stages side by side on
  the same index and the same candidate pool.

**Key concepts.**
- *Bi-encoder versus cross-encoder.* The embedding model encodes the query and each chunk
  separately; relevance is one dot product, which is why the whole corpus can be scored in
  milliseconds and why the model can never look at the two texts together. A cross-encoder
  takes the pair as one input and runs a full transformer over it, so it can notice that "convert
  minutes correctly" and a docstring saying "minutes are treated as hours" are about the same
  bug. That is a forward pass per pair, so it is only ever run on a shortlist: retrieve wide and
  cheap, then rerank narrow and expensive. Two stages is the standard shape of search systems
  since well before LLMs.
- *Scores are logits, not similarities.* MS MARCO cross-encoders output an unnormalised score,
  negative for irrelevant pairs (about -6 for the distractors in the smoke test, +0.1 for the
  right chunk). They sort well and threshold badly, which is why `Hit.score` carries them but
  nothing compares them across queries.
- *What the measurement said.* `uv run python evals/retrieval_eval.py --suite all --rerank`,
  40 tasks with gold files, `k=20` candidates, 1.5 minutes end to end on the CPU:

  | mode | recall@1 | recall@3 | recall@5 | MRR |
  |---|---|---|---|---|
  | hybrid | 0.08 | 0.99 | 1.00 | 0.56 |
  | dense | 0.08 | 0.99 | 1.00 | 0.56 |
  | bm25 | 0.08 | 0.99 | 1.00 | 0.55 |
  | hybrid+rerank | 0.50 | 0.99 | 1.00 | 0.76 |
  | dense+rerank | 0.50 | 0.99 | 1.00 | 0.76 |
  | bm25+rerank | 0.50 | 0.99 | 1.00 | 0.76 |

  Recall@3 was already 0.99, so on these small repos the first stage never *misses* the file; the
  problem was rank one. Without reranking, every first-stage mode puts the visible test file
  (`tests/test_durations.py`) ahead of the module it tests (`durations.py`): the prompt describes
  behaviour, and the test spells that behaviour out in the same words. Recall@1 is 0.00 on the
  30 HumanEval tasks and 0.33 on the ten in-house ones. The cross-encoder fixes exactly half of
  those cases (recall@1 to 0.50 on both suites, MRR 0.56 to 0.76) and the three reranked rows
  are identical because they rerank the same twenty candidates. The other half are still
  test-file-first, which is a fair result: the test *is* relevant to the task, and the planner
  reads both anyway.
- *Why it stays off by default.* The stage costs about 1.5 s per query (twenty pairs, six-layer
  model) on top of a retrieval that takes well under a second, and it changes which chunk is
  first, not which chunks are present, in a context block that already holds six. The number
  that would justify switching it on is a pass-rate or token difference on the agent benchmark,
  which the pending retrieval ablation is the place to measure; this step only shows that the
  ranking itself improves and that the mechanism is in place.

**How the real tools do it.** Cursor and Sourcegraph Cody both describe a retrieve-then-rerank
pipeline for codebase context, with a small reranker over the embedding hits before the prompt
is assembled; Cohere Rerank and Voyage rerank-2 are the hosted versions of the same model class.
Aider's repo map avoids the question by ranking files with graph centrality rather than a model.
LlamaIndex and LangChain expose the stage as a node postprocessor or a
`ContextualCompressionRetriever` around a `CrossEncoderReranker`, which is the same wrapper this
module is, minus the framework.

**Check it.**
```bash
uv run pytest tests/test_reranker.py tests/test_retrieval_eval.py -q      # 15 tests, no model download
uv run python evals/retrieval_eval.py --mode hybrid --mode hybrid+rerank   # two rows, about a minute
CODER_RERANK=true uv run coder run evals/suite/fix-bug-duration-units/repo "fix the duration parsing" -v
```
The first run downloads the 80 MB model to `~/.coder-agent/models`. With `CODER_RERANK=true`
the `Context ·` line before the plan lists `durations.py` first; without it the test file leads.

## Step 8.1 — CI: ruff and pytest on every push, a coverage badge with no third-party service

**What we built.** `.github/workflows/ci.yml` and `scripts/coverage_badge.py`.

- One `test` job on `ubuntu-latest`, a matrix over Python 3.11 and 3.12, four steps: check out,
  `astral-sh/setup-uv` with the matrix Python and the uv cache on, `uv sync --extra dev --frozen`,
  then `ruff check .` and `pytest --cov`. The 3.11 leg uploads `coverage.json` as a workflow
  artifact.
- A `badge` job that runs only on pushes to `main`, after both legs pass. It downloads the
  report, converts it with `scripts/coverage_badge.py` into shields.io's endpoint JSON
  (`{"schemaVersion": 1, "label": "coverage", "message": "88%", "color": "green"}`), and
  force-pushes that one file as an orphan `badges` branch using the workflow's own
  `GITHUB_TOKEN`. The README's coverage badge is
  `img.shields.io/endpoint?url=<raw URL of that file>`; the CI badge is the one GitHub serves for
  the workflow.
- `pytest-cov` joins the dev extras; `[tool.coverage.run] source = ["coder_agent"]` in
  `pyproject.toml` means `--cov` with no argument measures the package and nothing else. Three
  tests for the badge script, because it is the only code between the number and the README.

**Key concepts.**
- *Why `--frozen`.* `uv sync` on its own may re-resolve and quietly rewrite `uv.lock` if
  `pyproject.toml` moved ahead of it. In CI that would mean testing a dependency set nobody
  committed. `--frozen` installs exactly what the lock pins and fails if the two disagree, which
  turns "forgot to run `uv lock`" into a red build instead of drift.
- *What actually runs in CI that does not run locally.* GitHub's Ubuntu runners ship a Docker
  daemon, so the four sandbox integration tests from step 6.4 that skip on a laptop without
  Docker Desktop build the `coder-sandbox` image and run for real: no network, no host
  filesystem, writes land in the mounted repo. The suite needs no API key and downloads no
  model; every model and embedder in the tests is a fake, which is what made this step a
  configuration exercise rather than a mocking exercise.
- *A badge is a URL that returns an SVG.* GitHub renders the CI badge from the workflow's latest
  run on the default branch. Coverage has no built-in equivalent, and the usual answer (Codecov,
  Coveralls) is a third account and an upload token for a single integer. shields.io's
  *endpoint* badge renders any JSON that follows a five-field schema, fetched from any public
  URL, so the workflow publishes the JSON itself. An orphan branch is the cheapest public URL a
  repo already owns: `raw.githubusercontent.com/<owner>/<repo>/badges/coverage.json`, no Pages
  build, no gist token. Force-pushing a single-commit branch each time keeps the history a
  history of nothing.
- *Least privilege in the workflow.* The workflow-level `permissions: contents: read` strips
  the token to read-only for every job; only the `badge` job raises it to `contents: write`,
  and only pushes to `main` reach that job. A pull request from a fork gets a read-only token
  and never touches the branch. `concurrency` with `cancel-in-progress` means a second push
  while the first is still running cancels the stale run; the free tier is 2,000 minutes a month
  and a full run is several of them.
- *Where the coverage number is honest and where it is not.* 88% line coverage over
  `src/coder_agent`. The MCP server module is spawned as a subprocess by its own tests, so the
  lines it executes there are invisible to coverage running in the pytest process; the number
  under-reports that module (it shows 0% of 140 lines while its tests exercise every tool).
  Fixing it means `coverage`'s subprocess hooks, which is not worth it for one file and a badge.

- *What the first green run cost: two bugs the laptop never showed.* The suite had passed 307
  tests on Windows for weeks; the first Linux run failed two of them, and both were real.
  First, `typer` forces coloured help whenever `GITHUB_ACTIONS` is in the environment and
  splices escape codes into option names, so `"--yes" in result.output` was false; the test now
  strips ANSI before comparing. Second, and worse, `test_loop_reflects_and_fixes_on_second_iteration`
  ran to the iteration cap on Python 3.12 only. The buggy `return x * 3` and the fix
  `return x * 2` have the same length, and on a fast runner the fix landed in the same second
  as the first pytest run. CPython validates a cached `.pyc` by source mtime and size, both
  unchanged, so iteration 2 imported the bytecode of the buggy module and the tests failed
  again. That is not a test artefact: the agent edits a module and re-runs the tests seconds
  later, so any same-length fix could be shadowed in a real run. The Docker sandbox already set
  `PYTHONDONTWRITEBYTECODE=1`; the local sandbox now does too, with a regression test that
  pins the source mtime to force the collision. Slow machines hide timing bugs; CI is a second
  machine with different timing, which is a large part of its value.

**How the real tools do it.** Every open coding agent on GitHub (aider, OpenHands, SWE-agent,
Cline) runs lint and tests in GitHub Actions on push and pull request, most with a Python
matrix; aider and OpenHands publish to Codecov. `uv` in CI via `setup-uv` with the cache on is
the pattern Astral documents and that most `uv` projects have converged on since 2024. The
orphan-branch badge is a pattern from before Codecov existed and still common in projects that
refuse extra accounts; `genbadge` and `coverage-badge` are the pip-installable versions of the
forty lines in `scripts/coverage_badge.py`.

**Check it.**
```bash
uv run pytest --cov -q                       # the same command CI runs, prints the table
uv run python scripts/coverage_badge.py coverage.json /tmp/badge.json && cat /tmp/badge.json
gh run list --workflow ci.yml --limit 3      # green on main
gh api repos/Cha-Imaa/coder-agent/contents/coverage.json?ref=badges -q .content | base64 -d
```
The last line prints the JSON shields renders; the README badge updates within a few minutes of
a push to `main` (shields caches endpoint badges for about five minutes).

## Step 7.1 — A second MCP server: GitHub issues in, pull requests out

**What we built.** `mcp_server/github.py`, `fix_issue.py`, the `coder fix-issue` command, and
the client change that loads two servers into one tool list.

- The GitHub server is a second FastMCP process with two tools. `get_issue(owner, repo, number)`
  fetches an issue and its latest comments over the REST API and renders them as one Markdown
  block (title, labels, body capped at 8k characters, the last five comments). `open_pull_request`
  posts a pull request from an existing remote branch. Both are thin wrappers over a
  `GitHubAPI` class that owns the endpoint, the headers and the error wording; the same class
  is what the CLI uses directly, so the tool and the command cannot drift apart.
- `tools/client.py` grows a `github=True` switch. `MultiServerMCPClient` gets a second entry
  (`python -m coder_agent.mcp_server.github`), performs a second handshake, and returns one flat
  list. The client then drops `open_pull_request` before the model sees it: the write tool
  exists for other clients and for the command, not for the model.
- `fix_issue.py` is the pipeline around the agent: parse the URL (or `owner/repo#N`), fetch the
  issue, clone the repository under `~/.coder-agent/checkouts` or use `--repo`, check out
  `coder/issue-N`, run `run_agent` with the issue text as the task and the GitHub tools on,
  and, only with `--pr` and only when the tests pass, commit (`fix: <title> (#N)`), push and
  open the pull request with `Closes #N` in its body. Git runs on the host through
  `subprocess`, never through the sandbox.
- 35 tests. A local `FakeGitHub` HTTP server stands in for api.github.com so the MCP server
  runs over real stdio in tests; a bare git repository stands in for the remote so the push is
  real; the agent step is a fake that edits a file and reports a status. `GITHUB_API_URL`
  points the real server at the fake, which is also how the server would talk to GitHub
  Enterprise.

**Key concepts.**
- *Multiple servers, one tool list.* The model never learns that `read_file` and `get_issue` are
  answered by different processes: MCP tools are name plus JSON schema plus description, and
  `ToolNode` calls whichever `BaseTool` carries the name. That is what makes MCP a plug-in
  model rather than a library: adding a capability is adding a process to the config, and the
  graph, the prompt and the approval gate need no change. The one thing to watch is name
  collisions across servers; the adapter does not namespace, so two servers exposing
  `search` would shadow each other.
- *Credentials live with the server that needs them.* `GITHUB_TOKEN` is read by the GitHub
  server from its own environment. The file-tool server never sees it, and the model can never
  print it, because no tool returns it. This is the least-privilege argument for splitting
  servers by backend rather than by feature: each process holds exactly the secret for its API.
  For the `--pr` step the command holds the token too, because it is the one opening the
  request; the same variable, read at the same place, by the same class.
- *Which tools the model gets is client policy.* The server advertises `open_pull_request`
  because a human in an IDE or the MCP Inspector should be able to call it. The agent does not
  get it, for two reasons that reinforce each other. The sandbox denylist blocks `git push`, so
  the model could never create the remote branch a valid pull request needs. And a pull request
  under the user's name on someone else's repository is outward-facing; the design rule since
  step 6.2 is that such actions happen when the user asks, in a fixed order. `--pr` is that ask.
  Filtering on the client rather than removing the tool from the server keeps both uses.
- *The model does one arrow of the pipeline.* Parse, clone, branch, commit, push and open are
  deterministic and cheap to get right in Python; the model's job is the part that needs
  judgement, the fix. Putting the plumbing outside the graph means a failed run leaves a clean
  branch to inspect, a passed run without `--pr` leaves uncommitted edits to review, and the
  ledger tags the run `source=github, issue=<url>` so these runs can be grouped later. It also
  means the whole `fix-issue` path is testable without a model: the fake agent proves the
  plumbing, the eval suite proves the agent.
- *An issue is a bad task description, on purpose.* Issues are written for maintainers, not for
  a planner: screenshots, stack traces, "same here" comments. The rendering keeps the body and
  the latest comments because the reproduction steps and the maintainer's verdict on the right
  fix are usually there, clips both, and the task framing in front of it says what "done"
  means: reproduce with a test where practical, smallest change, existing tests still pass.
  Against a real issue (`pallets/flask#10`) the render came back as expected; against a pull
  request number the server refused with a message that names the mistake, since GitHub serves
  pull requests from the issues endpoint too.

**How the real tools do it.** Anthropic's reference `github` MCP server (now GitHub's own,
in Go) exposes the same shape at larger scale: dozens of read tools, a few write tools, one
`GITHUB_PERSONAL_ACCESS_TOKEN`, and clients such as Claude Desktop and Cursor merge it with
filesystem servers exactly as `MultiServerMCPClient` does here. OpenHands' "resolver" and
SWE-agent's GitHub mode are the `fix-issue` pipeline as a GitHub Action: triggered by a label,
they clone, run the agent, and open a pull request with the issue linked, with the agent never
holding push rights itself. Aider does the git half in-process (auto-commit per edit) but leaves
pushing to the user, the same line this step draws.

**Check it.**
```bash
uv run pytest tests/test_github_server.py tests/test_fix_issue.py -q     # 35 tests, no network
uv run python scripts/list_tools.py . --github                            # 7 tools, no open_pull_request
uv run python -m coder_agent.mcp_server.github                            # the server alone, for the Inspector
uv run coder fix-issue pallets/flask#10 --repo path/to/your/flask/clone   # any public issue, no token
```
For a real end-to-end run, use a repository you own: open an issue on it, then
`uv run coder fix-issue OWNER/REPO#N --pr --yes` with `GITHUB_TOKEN` set. The command prints
each stage (issue, checkout, agent, commit, push, pull request) and ends with the pull request
URL; the ledger row for the run carries `source=github`.

---

## Step 8.4 — Docs site: MkDocs Material on GitHub Pages

**What we built.** `mkdocs.yml`, two new pages under `docs/`, `.github/workflows/docs.yml`, and a
`docs` extra in `pyproject.toml`. The site is <https://cha-imaa.github.io/coder-agent/>.

- `mkdocs.yml` points `docs_dir` at the existing `docs/` folder, so the learning log, the system
  design and the plan are published as they are, without copies. Two pages are new:
  `docs/index.md` (what the project is, the stack, how to read the site, quick start) and
  `docs/results.md` (every number the README quotes, plus the retrieval and reranker tables from
  steps 5.4 and 7.3 and a list of what is still being measured). The figures under
  `docs/figures/` are already inside `docs_dir`, so the pages reference them as `figures/x.png`.
- Material theme with a light/dark toggle, section navigation, code-copy buttons and an "edit
  this page" link back to GitHub. The markdown extensions are the usual Material set:
  admonitions, task lists (the plan's checkboxes render as checkboxes), fenced code with
  highlighting, and a `mermaid` fence for the architecture diagram the README will get in
  step 8.2.
- The workflow has two jobs. `build` runs read-only: `uv sync --extra docs --frozen`, then
  `mkdocs build --strict`, then uploads the `site/` directory as a Pages artifact. `deploy`
  needs `build` and is the only job with `pages: write` and `id-token: write`; it hands the
  artifact to `actions/deploy-pages`. It triggers on pushes to `main` that touch `docs/`,
  `mkdocs.yml` or the lock, and on manual dispatch.
- GitHub Pages was switched to the "GitHub Actions" source with one API call,
  `gh api -X POST repos/<owner>/<repo>/pages -f build_type=workflow`, instead of the settings
  page. `mkdocs-material` lives in its own `docs` extra so the agent's install never pulls in a
  static-site generator; `uv lock` pins it (mkdocs 1.6.1, Material 9.7.7).
- Three tests in `tests/test_docs_site.py` read `mkdocs.yml` as text: every nav entry exists,
  every `docs/*.md` is in the nav, and the workflow builds strict from the lock with a read-only
  token. They run without mkdocs installed, which is the point: the dev environment does not
  carry the docs extra, and CI's `--strict` build is the real gate.

**Key concepts.**
- *`--strict` is the docs equivalent of a failing test.* MkDocs treats a broken relative link, a
  missing anchor or a page absent from the nav as a warning and still writes the site. With
  `--strict` each of those is a non-zero exit, so a renamed file breaks the build instead of
  shipping a 404. Locally the build took 0.64 seconds for five pages, so there is no reason not
  to run it strict everywhere.
- *Why a workflow and not `mkdocs gh-deploy`.* `gh-deploy` builds on the laptop and force-pushes
  the HTML to a `gh-pages` branch, so the published site reflects whatever working tree the
  developer happened to have. Building from `main` in Actions means the site can never be ahead
  of or behind the repository, and the deploy job's token comes from GitHub's OIDC provider
  (`id-token: write`) rather than a long-lived secret. The build job keeps `contents: read`;
  the same least-privilege split as the CI workflow in step 8.1.
- *Deployments do not cancel each other.* CI uses `cancel-in-progress: true` because a stale
  test run has no value. The Pages workflow uses a `pages` concurrency group with
  `cancel-in-progress: false`: a half-finished deployment cancelled mid-flight can leave the
  site in an odd state, so a second push simply queues behind the first.
- *`site_url` is not decoration.* MkDocs needs the final URL to write absolute links in the
  sitemap and canonical tags, and Material uses it for the search index and social cards. On a
  project page the URL is `https://<owner>.github.io/<repo>/`, lowercase owner, trailing slash.
- *One source, three readers.* The README stays the short pitch for someone who lands on
  GitHub; the docs site is where the long-form learning log becomes navigable (a 1,800-line
  Markdown file with a table of contents and search is a different object from the same file in
  a code viewer); and the same `docs/` folder is what the acceptance walkthrough in step 9
  lands in as "Try it".
- *A warning worth reading.* Material prints a notice at build time that MkDocs 2.0 will remove
  the plugin system and that Material will not follow it. Nothing to do today: the lock pins
  MkDocs 1.6.1 and Material 9.7.7, and `--frozen` in the workflow means the site builds with
  exactly those until someone chooses to upgrade.

**How the real tools do it.** Material for MkDocs is the default choice in the Python tooling
world: FastAPI, Typer, Pydantic, uv and ruff all publish their docs with it, and the LangGraph
docs were built on it through 2025. The deploy pattern (build job uploads with
`upload-pages-artifact`, separate job calls `deploy-pages` under the `github-pages` environment)
is the one GitHub's own starter workflows use for every static site generator. Larger agent
projects go further, with versioned docs (`mike`), API reference generated from docstrings
(`mkdocstrings`) and link checkers in CI; the shape is the same, more plugins.

**Check it.**
```bash
uv sync --extra docs
uv run mkdocs serve                           # http://127.0.0.1:8000 with live reload
uv run mkdocs build --strict                  # the command CI runs; exit code 0 or nothing ships
uv run pytest tests/test_docs_site.py -q      # nav and workflow checks, no mkdocs needed
gh run list --workflow docs.yml --limit 3     # build then deploy, green on main
gh api repos/Cha-Imaa/coder-agent/pages -q .html_url
```
The last line prints the site URL; the first deployment appears there a minute or two after
the workflow's `deploy` job finishes.

## Step 8.2 — README: the pitch, two diagrams, the numbers, and a test that keeps them honest

**What we built.** A rewritten `README.md` and `tests/test_readme.py`. The README is the page a
recruiter or a reviewer sees first, so it has to answer four questions in the order they are
asked: what is this, how do I run it, how does it work, does it work.

- *Quick start first.* Clone, `uv sync`, copy `.env.example`, five commands. A table of the
  `coder run` flags that change behaviour (`--yes`, `--sandbox docker`, `--resume`, `--model`,
  `--max-iterations`, `--test-command`) with one line each, taken from the Typer help strings so
  the two cannot drift far apart.
- *Two Mermaid diagrams.* The first is the graph itself, drawn from `graph/build.py`: every node,
  every conditional edge with the condition as the edge label (`tool calls`, `model stopped`,
  `red, iterations left`, `step budget spent`), and the `approve` detour that only exists when
  the run was started without `--yes`. The second is the component view around the graph: CLI,
  the two MCP servers, the sandbox the file server dispatches into, the retrieval stack, the LLM
  chain and the ledger. A table under the first diagram says in one line what each node does,
  checked against the node docstrings (for example `reflect` makes no model call and `finish`
  does not write the ledger; the caller in `agent.py` does).
- *Results, copied from `docs/results.md`, not retyped.* The pass-rate table, the three figures,
  the retrieval table reduced to the two rows that carry the message (hybrid with and without
  the reranker), and a "still being measured" list so nobody mistakes the suite score for a
  benchmark result. The hero GIF moved to step 8.3, where the recording script lives; a README
  should not link an image that does not exist yet.
- *Project layout.* One line per package under `src/coder_agent/`, so a reader can go from the
  diagram to the file that implements a box.
- *Three tests in `tests/test_readme.py`.* Every relative link and image target exists on disk;
  every figure under `docs/figures/` is shown (the ablation figure is exempt until its arms are
  run); code fences are balanced and there are exactly two `mermaid` blocks. The second test
  failed on the first run because the iteration curve was missing from the README, which is
  the point of having it.

**Key concepts.**
- *A README is documentation with a funnel.* Most visitors leave after the first screen, so the
  first screen carries the one-paragraph pitch, the stack table and the docs link; the quick
  start is next because a reader who can run it will forgive a lot; the architecture comes
  after, for the reader who stayed; the numbers last, because they only mean something once
  the reader knows what was measured. The order is the same one a good paper abstract uses.
- *Diagrams from code, not from memory.* Both diagrams were drawn by reading `build.py`,
  `nodes.py` and `approval.py` and then checked line by line against them. Two claims written
  from memory turned out wrong (`reflect` summarising, `finish` writing the ledger) and were
  fixed before commit. A diagram that is slightly wrong is worse than none, because it is
  trusted more than prose.
- *Mermaid renders on GitHub and in MkDocs Material.* GitHub renders ` ```mermaid ` fences
  natively since 2022, and step 8.4 registered the same fence in `mkdocs.yml`, so one source
  serves both. Labels with spaces or punctuation go in quotes; `<br/>` is the line break inside
  a node; `⇄` and `→` are plain Unicode and render fine, which keeps the labels short.
- *The tests are about drift.* Nothing in CI reads the README, so the only way a renamed figure
  or a moved doc breaks the build is a test that resolves the relative targets. The same idea
  as the nav tests of step 8.4: a documentation file is data, and data gets validated.
- *Numbers in two places is one place too many.* The README's tables are copies of
  `docs/results.md`, which is the source of truth because it is where the scripts' output is
  pasted first. When the ablation and HumanEval numbers land, both files change in the same
  commit, and the "still being measured" list shrinks.

**How the real tools do it.** Open-source agent repositories converge on the same README shape:
badges, a two-line pitch, a GIF or screenshot, install, usage, architecture, benchmark table,
links out. Aider's README leads with its benchmark numbers and a chart because the numbers are
its argument; OpenHands and SWE-agent lead with a screenshot and a one-command start because
the experience is theirs; LangGraph's README leads with a code snippet because the API is the
product. The architecture diagram as Mermaid in the README (rather than a PNG that goes stale)
is the pattern used by uv, ruff and most of the LangChain ecosystem. Link checkers such as
`lychee` or `markdown-link-check` in CI are the grown-up version of `test_readme.py`.

**Check it.**
```bash
uv run pytest tests/test_readme.py -q       # links, figures, fences
gh repo view Cha-Imaa/coder-agent --web     # GitHub renders the two Mermaid diagrams inline
```
On the repository page, the graph diagram shows nine nodes and the `approve` detour; the
component diagram shows the file server feeding the sandbox and the graph feeding the ledger.
