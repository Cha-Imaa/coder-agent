## What

One or two sentences: what changes and why. Link the issue if there is one.

## Checklist

- [ ] `uv run ruff check .` and `uv run pytest -q` pass locally
- [ ] Tests added or updated, or the description says why not
- [ ] Filesystem and shell actions go through `src/coder_agent/sandbox/`
- [ ] If a roadmap step is finished: ticked in `docs/PLAN.md`, entry added to `docs/TEACH.md`
- [ ] `CHANGELOG.md` updated under *Unreleased* for anything a user would notice
- [ ] No API keys, local paths or personal data in the diff (check `evals/results/` too)
