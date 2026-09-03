# Try it

A walkthrough that starts from an empty directory and ends with the agent opening a pull request.
It is also the project's acceptance checklist: every command here is run on a fresh clone before a
release is tagged, and anything that goes wrong becomes an issue.

Roughly twenty minutes, most of it waiting on the model. Everything runs on free tiers or on your
own machine.

## What you need

| | |
|---|---|
| Python 3.11 or 3.12 | The lock file is resolved for both |
| [`uv`](https://docs.astral.sh/uv/) | Installs the environment from `uv.lock` |
| A [Groq](https://console.groq.com) API key | Free tier, no card. The default model |
| A [Google AI Studio](https://aistudio.google.com/apikey) key | Free tier. Used only when Groq rate-limits |

Optional: a `GITHUB_TOKEN` for the pull-request step, Docker Desktop for the container sandbox,
a [K2 Think](https://www.k2think.ai) key if you want the 10M-tokens-a-day quota for running the
whole eval suite (`K2_API_KEY`, then `--model k2think:MBZUAI-IFM/K2-Think-v2`), and
[Ollama](https://ollama.com) if you would rather run a model locally and use no quota at all.

## 1. Install

```bash
git clone https://github.com/Cha-Imaa/coder-agent.git
cd coder-agent
uv sync --extra dev
cp .env.example .env      # then fill in GROQ_API_KEY and GOOGLE_API_KEY
```

Check the install without spending a single token:

```bash
uv run pytest             # the whole suite, no API key needed
uv run ruff check .
```

!!! note "Windows: keep the checkout path short"
    A few HumanEval task files have relative paths of about 110 characters. In a deeply nested
    directory that can cross the 260-character `MAX_PATH` limit and `git clone` fails with
    `Filename too long`. Clone somewhere shallow, or run
    `git config --global core.longpaths true` once.

## 2. Index a repository

Retrieval is local: the embedding model runs on your CPU and the vectors go in a Chroma store
under the target repo's `.coder-agent/`. No key, no network after the first model download.

```bash
uv run coder index evals/suite/fix-bug-duration-units/repo --query "where are durations parsed"
```

You should see the files indexed, the chunk count, and the top five hits with their
`path:lines (symbol)` locations. Re-running only re-embeds files whose hash changed.

## 3. Fix a failing test

`evals/suite/` holds small repositories that each have a real bug and a failing test. Copy one
somewhere writable first, so the suite stays pristine:

```bash
cp -r evals/suite/fix-bug-duration-units/repo /tmp/duration-demo
uv run coder run /tmp/duration-demo "make the failing tests pass"
```

Point the agent at the task's `repo/`, not at the task directory: the latter also contains
`hidden_tests/`, which is the grader and which the agent is never supposed to see.

What happens, in order: `retrieve_context` puts the relevant chunks in front of the planner,
`plan` writes a short plan, `act` reads files and proposes an edit, **you are shown a diff and
asked to approve it**, then `run_tests` runs pytest and the loop either finishes or reflects on
the failure and tries again. Approve with `y`; `--yes` skips the prompt entirely.

If the run stops - a declined edit, a rate limit, a closed laptop - it prints a thread id:

```bash
uv run coder run /tmp/duration-demo --resume <thread>
```

## 4. Several tasks on one thread

```bash
uv run coder chat /tmp/duration-demo
```

The same graph, kept open. Ask for a change, let it finish, then ask for another in the same
session; the second turn still has the first one's context. `/exit` leaves.

## 5. An issue in, a pull request out

Reading a public issue needs no token. Opening the pull request needs a `GITHUB_TOKEN` with
Contents and Pull requests read/write on the target repository — use a throwaway repo of your own
for this.

```bash
uv run coder fix-issue https://github.com/OWNER/REPO/issues/1 --repo path/to/your/clone
uv run coder fix-issue https://github.com/OWNER/REPO/issues/1 --repo path/to/your/clone --pr
```

The fix lands on a `coder/issue-1` branch. With `--pr` it is committed, pushed and opened as a
pull request, but only once the tests pass.

## 6. Run the agent's commands in a container

With Docker Desktop running:

```bash
uv run coder run /tmp/duration-demo "make the failing tests pass" --sandbox docker
```

Every shell command now runs in a fresh container with no network, a memory and CPU cap, and the
repository as its only mount. The image is built from `src/coder_agent/sandbox/Dockerfile` the
first time, which takes a minute.

## 7. Reproduce the numbers

```bash
uv run python evals/run_evals.py
uv run python evals/figures.py
```

`run_evals.py` materialises an isolated copy per task, runs the agent, installs the hidden tests
and grades. Results go to `evals/results/` as JSON; `figures.py` redraws every PNG in
`docs/figures/` from those files.

By default it runs the **quick subset**: the cheapest task in each of four categories, about
57,000 tokens and five minutes. That is deliberately not the headline number, so its results file
is labelled `-quick` and the figures skip it.

!!! warning "The full suite is a day's quota"
    Groq's free tier allows 200,000 tokens per day. All twelve tasks cost about 356,000, so
    `--full` does not fit in one day: a measured run spent 199,198 tokens on eight tasks and the
    last four died on 429s. When that happens, pass `--rerun-errors <previous.json>` the next day
    to top up the tasks that hit the limit and merge them into the same results file.

    ```bash
    uv run python evals/run_evals.py --full
    uv run python evals/run_evals.py --rerun-errors evals/results/<the-file>.json   # next day
    ```

## Using no quota at all

Install [Ollama](https://ollama.com), pull a tool-calling model, and point one flag at it:

```bash
uv sync --extra ollama
ollama pull qwen2.5-coder:7b
uv run coder run /tmp/duration-demo "make the failing tests pass" --model ollama:qwen2.5-coder:7b
```

No key and no quota, at the price of speed and of a noticeably weaker model — see the model
comparison on the [results page](results.md) for what a 7B on a laptop CPU actually manages.
Cloud keys are not required for this path; if `CODER_FALLBACK_MODEL` is still set and its
provider is not configured, the fallback is dropped with a warning rather than failing the run.

## If something goes wrong

| Symptom | Cause |
|---|---|
| `RateLimitError ... tokens per day (TPD)` | Groq's daily allowance is gone, and the Gemini fallback could not absorb it either. Wait for the window, or switch to Ollama |
| `Filename too long` on clone | Windows `MAX_PATH`; see the note in step 1 |
| `docker daemon is not running` | Start Docker Desktop, or drop `--sandbox docker` |
| Run stops at an approval prompt | That is the point; answer `y`, or pass `--yes` |
| A local model narrates an edit instead of making it | Small models sometimes write tool calls as prose. The agent recovers the common shapes; a model without tool-calling support will still not work |

Anything else is worth [an issue](https://github.com/Cha-Imaa/coder-agent/issues).
