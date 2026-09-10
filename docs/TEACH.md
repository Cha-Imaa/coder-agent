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
  models that needs no torch install; the default model is `BAAI/bge-small-en-v1.5` (67 MB, 384
  dimensions), downloaded once into `~/.coder-agent/models`. `HashEmbedder` is a bag-of-words
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
