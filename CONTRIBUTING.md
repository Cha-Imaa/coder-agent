# Contributing

This is a learning and portfolio project, but it is built like a real one: every change comes
with tests, lint passes, and the docs move in the same commit as the code. Issues and pull
requests are welcome; small, focused ones are merged fastest.

## Set up

```bash
git clone https://github.com/Cha-Imaa/coder-agent.git
cd coder-agent
uv sync --extra dev
cp .env.example .env      # only needed to run the agent; the tests need no keys
```

Python 3.11 or 3.12, managed by [uv](https://docs.astral.sh/uv/). `uv.lock` is the source of
truth for versions: CI installs with `--frozen`, so run `uv lock` and commit the lock when you
change `pyproject.toml`.

## Before you open a pull request

```bash
uv run ruff check .                        # lint, same command CI runs
uv run pytest -q                           # 350+ tests, no network, no API key, about 4 minutes
uv run pytest tests/test_docker_sandbox.py # runs for real only if a Docker daemon is up
```

- Tests live in `tests/`, one file per module, with fakes in `tests/fakes.py` rather than mocks
  of network calls. A change to `src/` without a test needs a sentence in the PR saying why.
- Every filesystem or shell action the agent performs must go through `src/coder_agent/sandbox/`.
  A tool that touches disk or runs a command anywhere else will not be merged.
- Type hints on every function; docstrings say *why*, not *what*.
- Line length is 100 (`ruff` enforces it).

## Commit messages

Conventional style, imperative, lower case, no trailing period:

```
feat: hybrid retriever fusing BM25 and dense ranks
fix: local sandbox stops Python writing bytecode
docs: learning log entry for the reranker step
test: fake GitHub reads the request body before answering
chore: pin setup-uv to an exact release
```

One roadmap step per commit where possible. If your change finishes a step in `docs/PLAN.md`,
tick it in the same commit and add an entry to `docs/TEACH.md` in the existing format: what was
built, why this way, how production coding agents do the same thing, and a command that checks
it. The log is written for someone learning the stack, not for someone who already knows it.

## Filing an issue

Use the templates: a bug report asks for the command, the output and the model that answered
(`coder stats` prints it); a feature request asks what you would do with it. Evaluation results
are welcome too: if you run `evals/run_evals.py` with a different model, open an issue with the
JSON from `evals/results/` attached.

## Cost

The project runs on free tiers only (Groq, Gemini, LangSmith, local embeddings). A change that
needs a paid service, or that pushes a routine run past the free quotas, should say so in the
PR so it can be made optional.

## Licence

By contributing you agree that your contributions are licensed under the MIT licence in
`LICENSE`.
