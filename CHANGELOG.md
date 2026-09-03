# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/). Dates are the day the change landed on `main`.

## [Unreleased]

The first release, `0.1.0`, is tagged once the acceptance walkthrough in `docs/PLAN.md`
(milestone 9) has been run on a fresh clone and every rough edge it finds is fixed.

### Added
- **Parallel eval runner** (2026-09-16): `evals/run_evals.py --parallel N` runs N tasks at
  once, each in its own repository copy, checkpoint file and tool-server process. A worker
  whose task hit a rate limit retires, so a run that starts too wide narrows itself instead of
  feeding 429s to the fallback; how many retired is recorded in the results file, and every
  task result now carries the provider errors its run saw. Grading runs in a thread so one
  task's pytest does not stall another's model call. The full suite is the default again and
  `--quick` opts into the four-task subset (`--full` is still accepted).
- **K2 Think as a fourth provider** (2026-09-16): `--model k2think:MBZUAI-IFM/K2-Think-v2`
  (or `CODER_MODEL`) runs the graph against MBZUAI's hosted reasoning model through its
  OpenAI-compatible endpoint, with `K2_API_KEY` and `CODER_K2_REASONING_EFFORT`. Its quota is
  10M tokens a day, so a full eval arm no longer waits on Groq's 200k. The context budget is
  capped per provider window (`CODER_CONTEXT_WINDOWS`, `CODER_CONTEXT_RESERVE_TOKENS`) so a
  64k-window model is not sent a 60k conversation. A transport under the openai client
  rewrites the two request shapes the endpoint refuses: a quoted interpreter call such as
  `` `python -m pytest` `` anywhere in the body (its firewall answers 403) and tool results sent
  as a list of text blocks (400).
- **Ollama as a third provider** (2026-09-11 to 2026-09-14): `--model ollama:<tag>` runs the
  same graph against a model served locally, with the daemon's address and context size as
  settings (`uv sync --extra ollama`). Tool calls that a small local model writes as JSON in the
  message text, instead of as a structured call, are parsed back into real calls before the
  router sees the reply, so a 7B model can drive the loop; replies that already carry structured
  calls are untouched. `evals/figures.py` draws `model_comparison.png` (pass rate, tokens
  and seconds per task per model) once two models have run the in-house suite, and no longer
  trips over the retrieval eval's JSON in the same results directory.
- **Demo recorder** (2026-09-11): `scripts/demo.py` records a `coder run` through a pipe to an
  asciinema v2 cast, answering the approval prompt, and renders the cast to a GIF with Rich and
  Pillow, no ffmpeg or asciinema needed; `scripts/record_demo.ps1` does both on a fresh copy of
  a suite task. The CLI now honours `FORCE_COLOR`, which on Windows is the difference between
  ANSI colours and no colours at all when stdout is not a console. `docs/figures/demo.gif`, a
  real run of `fix-bug-duration-units`, is the README hero.
- **Agent loop** (2026-09-09): LangGraph state graph with `prepare`, `retrieve_context`, `plan`,
  `act`, `tools`, `run_tests`, `reflect` and `finish` nodes; the test command is the ground
  truth, up to four plan/act/test iterations; tool-output trimming and conversation
  summarisation when the window fills.
- **Tools over MCP** (2026-09-08): a stdio server exposing `read_file`, `edit_file`,
  `write_file`, `list_dir`, `search_code` and `run_command`, every path resolved inside the
  repository and every command behind a denylist, a timeout and output truncation.
- **CLI** (2026-09-09 to 2026-09-11): `coder run` with streamed plan, tool calls and diffs;
  `coder chat` for several tasks on one checkpointed thread; `coder index` to build and query
  the retrieval index; `coder stats` for tokens and fallbacks per run; `coder fix-issue` to run
  the agent on a GitHub issue and open the pull request with `--pr`.
- **Providers** (2026-09-08, 2026-09-11): Groq primary and Gemini fallback selected by one env
  var; retry with backoff on the primary; the ledger records which model answered each call.
- **Telemetry** (2026-09-09): SQLite ledger of tokens per node, latency, iterations and outcome
  per run; LangSmith tracing.
- **Evaluation** (2026-09-09 to 2026-09-11): 12-task in-house suite with hidden tests and
  mutation grading; 30 HumanEval problems packaged as repository tasks; isolated runner with
  `--rerun-errors`, `--retrieval` ablation arms and `--agent noop` control; recall@k retrieval
  eval; `figures.py` drawing pass rate, iteration curve, cost profile and ablation charts.
- **Retrieval** (2026-09-10 to 2026-09-11): repository loader honouring nested `.gitignore`,
  tree-sitter chunking by definition, incremental Chroma index keyed by file hash, local
  embeddings on CPU, BM25 + dense with reciprocal rank fusion, optional cross-encoder reranker.
- **Memory and control** (2026-09-11): SQLite checkpointer with `--resume <thread>`; human
  approval via `interrupt()` before edits and commands, `--yes` to skip.
- **Docker sandbox** (2026-09-11): `--sandbox docker` runs commands in a throwaway container
  with no network and the repository as its only mount.
- **GitHub MCP server** (2026-09-11): `get_issue` and `open_pull_request` tools behind the same
  client as the file server.
- **CI and docs** (2026-09-11): GitHub Actions with ruff and pytest on Python 3.11 and 3.12,
  coverage badge from an orphan branch; MkDocs Material site on GitHub Pages; README with
  architecture diagrams and results; MIT licence, contributing guide, issue and PR templates.

### Changed
- `evals/run_evals.py` runs a four-task quick subset by default instead of the whole in-house
  suite: the cheapest task in each of four categories, about 57k tokens and five minutes against
  356k and half an hour. The Groq free tier allows 200k tokens per day, so a full pass is the
  whole day's budget and now has to be asked for with `--full`. Quick results are labelled
  `-quick` and carry `subset: quick` in their metadata, and every figure ignores them, so a
  four-task pass rate cannot be mistaken for the twelve-task number the README quotes.
  `--task`, `--category` and `--rerun-errors` are unaffected (2026-09-14).

### Fixed
- A local run no longer needs cloud keys: `--model ollama:<tag>` on a machine with no
  `GOOGLE_API_KEY` died before the first token, because the fallback model is constructed
  eagerly while it is only ever used after the primary fails. A fallback whose provider is not
  configured is now dropped with a warning instead of raising (2026-09-14).
- LangSmith tracing switches itself off when no API key is set, instead of printing a 401
  traceback for every batch of runs. `.env.example` ships `LANGSMITH_TRACING=true` so tracing
  starts the moment a key is pasted in, which meant a first run on the copied file scrolled past
  mostly authentication errors (2026-09-14).
- `coder run` survives a stdout that cannot encode the interface: redirecting the output to
  a file on a Western Windows install gave the process a cp1252 stdout, and the first
  re-planning arrow ended the run with `UnicodeEncodeError` from inside Rich (2026-09-14).
- Local sandbox stops Python writing bytecode, so a same-length edit is not shadowed by a stale
  `.pyc` (2026-09-11).
- `setup-uv` pinned to an exact release; the action has no `v10` major tag (2026-09-11).

[Unreleased]: https://github.com/Cha-Imaa/coder-agent/commits/main
