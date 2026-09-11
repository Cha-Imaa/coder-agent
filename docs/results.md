# Results

Every number here comes from a script in the repository and a results file under
`evals/results/`, so it can be regenerated after any change. The figures are drawn by
`evals/figures.py` from those files.

## Pass rate on the in-house suite

Twelve tasks with hidden tests across five categories, run with `groq:openai/gpt-oss-120b`, up to
four plan/act/test iterations, retrieval on. Every task was solved in one cycle. Six tasks first
errored on the free-tier quota (Groq allows 200k tokens per rolling 24 hours) and were rerun with
`--rerun-errors`; the results file records both commits.

| Category | Tasks | Passed | Errors | pass@1 | Avg tokens | Avg iterations |
|---|---|---|---|---|---|---|
| fix-bug | 3 | 3 | 0 | 100% | 12,443 | 1.0 |
| add-feature | 3 | 3 | 0 | 100% | 28,984 | 1.0 |
| refactor | 2 | 2 | 0 | 100% | 25,451 | 1.0 |
| add-test | 2 | 2 | 0 | 100% | 19,222 | 1.0 |
| multi-file | 2 | 2 | 0 | 100% | 71,164 | 1.0 |
| total | 12 | 12 | 0 | 100% | 29,663 | 1.0 |

![pass@1 by task category](figures/pass_rate.png)

The suite is small and the tasks are single-purpose, so 100% says the loop works, not that the
agent is done. A `--agent noop` run that touches nothing fails all twelve, so there are no free
passes in the suite.

![Share of tasks solved after each iteration](figures/iteration_curve.png)

## Where the tokens go

Almost the whole budget is spent in the `act` node reading files and running tools: about 97% of
tokens per run, around 29k against under 1k for `plan`. That is the number codebase retrieval is
meant to move.

![Tokens per run by graph node](figures/cost_profile.png)

## Retrieval quality

`evals/retrieval_eval.py` uses each task prompt as the query and scores the distinct files in hit
order against the gold files of the task. 40 tasks (ten in-house, thirty HumanEval), local
embeddings (`snowflake-arctic-embed-xs`), reranker `Xenova/ms-marco-MiniLM-L-6-v2` over twenty
candidates.

| mode | recall@1 | recall@3 | recall@5 | MRR |
|---|---|---|---|---|
| hybrid | 0.08 | 0.99 | 1.00 | 0.56 |
| dense | 0.08 | 0.99 | 1.00 | 0.56 |
| bm25 | 0.08 | 0.99 | 1.00 | 0.55 |
| hybrid + rerank | 0.50 | 0.99 | 1.00 | 0.76 |
| dense + rerank | 0.50 | 0.99 | 1.00 | 0.76 |
| bm25 + rerank | 0.50 | 0.99 | 1.00 | 0.76 |

Two things to read out of this table. First, the benchmark repositories have three to five files,
so file-level recall saturates at k=3 and the three first-stage modes cannot be told apart on this
corpus. Second, without reranking the file ranked first is almost always the visible test file,
because the prompt describes behaviour and the test spells it out in the same words; the
cross-encoder moves the source module to rank one in half of those cases and lifts MRR from 0.56
to 0.76. The reranker stays off by default: it costs about 1.5 seconds per query and changes which
chunk is first, not which chunks are present, in a context block that already holds six.

## Provider fallback

The ledger records which model answered each call. Before the retry layer was added, the Gemini
fallback answered at least one call in 12 of 17 runs, and 25 of 191 calls overall (13%) went to
Gemini, most of them a single Groq `tool_use_failed` glitch handed straight to the fallback.
`coder stats` prints the current numbers.

## Still being measured

- **Retrieval ablation** (pass rate and tokens with retrieval off, BM25, dense, hybrid): the
  `off` arm has five of twelve tasks graded so far; the other arms wait on the daily quota.
  The figure is drawn automatically once two or more arms are complete.
- **HumanEval slice** (thirty problems packaged as repository tasks): needs a full quota window.
- **Model comparison** (Groq, Gemini, a local Ollama model): the Ollama provider is wired
  (`run_evals.py --model ollama:qwen2.5-coder:7b`); `figures.py` draws pass rate, tokens and
  seconds per task side by side once a second model has run the in-house suite.
