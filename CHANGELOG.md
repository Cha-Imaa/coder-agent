# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/). Dates are the day the change landed on `main`.

## [Unreleased]

The first release, `0.1.0`, is tagged once the acceptance walkthrough in `docs/PLAN.md`
(milestone 9) has been run on a fresh clone and every rough edge it finds is fixed.

### Added
- **Ollama as a third provider** (2026-09-11): `--model ollama:<tag>` runs the same graph
  against a model served locally, with the daemon's address and context size as settings
  (`uv sync --extra ollama`). `evals/figures.py` draws `model_comparison.png` (pass rate, tokens
  and seconds per task per model) once two models have run the in-house suite, and no longer
  trips over the retrieval eval's JSON in the same results directory.
- **Demo recorder** (2026-09-11): `scripts/demo.py` records a `coder run` through a pipe to an
  asciinema v2 cast, answering the approval prompt, and renders the cast to a GIF with Rich and
  Pillow, no ffmpeg or asciinema needed; `scripts/record_demo.ps1` does both on a fresh copy of
  a suite task. The CLI now honours `FORCE_COLOR`, which on Windows is the difference between
  ANSI colours and no colours at all when stdout is not a console.
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

### Fixed
- Local sandbox stops Python writing bytecode, so a same-length edit is not shadowed by a stale
  `.pyc` (2026-09-11).
- `setup-uv` pinned to an exact release; the action has no `v10` major tag (2026-09-11).

[Unreleased]: https://github.com/Cha-Imaa/coder-agent/commits/main
