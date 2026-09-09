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
