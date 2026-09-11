"""Per-run ledger: what every run cost and how it ended, in a local SQLite file.

LangSmith holds the full traces. The ledger holds the numbers we want to aggregate offline:
tokens per node, wall time, iterations, outcome, which model actually answered. The eval runner
and `coder stats` read it; the figures in the README are drawn from it. SQLite because it is in
the standard library, survives crashes, and a single file is easy to copy or commit as a result.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

# usage shape stored in graph state: {node: {"calls", "input_tokens", "output_tokens", "models": {name: n}}}
Usage = dict[str, dict[str, Any]]


def usage_from_message(node: str, message: AIMessage) -> Usage:
    """Extract one model call's cost from the AI message it produced."""
    meta = message.usage_metadata or {}
    model = message.response_metadata.get("model_name") or message.response_metadata.get("model", "?")
    return {
        node: {
            "calls": 1,
            "input_tokens": int(meta.get("input_tokens", 0)),
            "output_tokens": int(meta.get("output_tokens", 0)),
            "models": {str(model): 1},
        }
    }


def merge_usage(left: Usage | None, right: Usage | None) -> Usage:
    """State reducer: sum per-node counters. Used as the `Annotated` reducer for `state.usage`."""
    out: Usage = {k: {**v, "models": dict(v.get("models", {}))} for k, v in (left or {}).items()}
    for node, add in (right or {}).items():
        cur = out.setdefault(node, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "models": {}})
        for key in ("calls", "input_tokens", "output_tokens"):
            cur[key] = cur.get(key, 0) + add.get(key, 0)
        for model, n in add.get("models", {}).items():
            cur["models"][model] = cur["models"].get(model, 0) + n
    return out


def totals(usage: Usage) -> tuple[int, int, int]:
    """(calls, input_tokens, output_tokens) across nodes."""
    calls = sum(v.get("calls", 0) for v in usage.values())
    inp = sum(v.get("input_tokens", 0) for v in usage.values())
    outp = sum(v.get("output_tokens", 0) for v in usage.values())
    return calls, inp, outp


@dataclass
class RunRecord:
    repo: str
    task: str
    model: str
    status: str
    iterations: int
    steps: int
    wall_seconds: float
    tests_passed: bool | None
    test_command: str | None
    usage: Usage = field(default_factory=dict)
    tags: dict[str, Any] = field(default_factory=dict)
    # Failed model attempts by exception type, from `ProviderEvents`; the calls that then went to
    # the fallback are visible in `usage[node]["models"]` and summed by `fallback_calls`.
    provider_errors: dict[str, int] = field(default_factory=dict)
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: float = field(default_factory=time.time)

    @classmethod
    def from_state(
        cls,
        state: dict[str, Any],
        *,
        model: str,
        wall_seconds: float,
        tags: dict | None = None,
        provider_errors: dict[str, int] | None = None,
    ) -> RunRecord:
        return cls(
            repo=str(state.get("repo", "")),
            task=str(state.get("task", "")),
            model=model,
            status=str(state.get("status", "unknown")),
            iterations=int(state.get("iteration", 0)),
            steps=int(state.get("steps", 0)),
            wall_seconds=wall_seconds,
            tests_passed=state.get("tests_passed"),
            test_command=state.get("test_command"),
            usage=state.get("usage", {}) or {},
            tags=tags or {},
            provider_errors=provider_errors or {},
        )

    @property
    def total_tokens(self) -> int:
        _, i, o = totals(self.usage)
        return i + o

    @property
    def fallback_calls(self) -> int:
        return fallback_calls(self.usage, self.model)

    def footer(self) -> str:
        """One line for the end of a CLI run."""
        calls, i, o = totals(self.usage)
        models = sorted({m for v in self.usage.values() for m in v.get("models", {})})
        line = (
            f"{calls} model calls · {i:,} in / {o:,} out tokens · {self.wall_seconds:.0f}s · "
            f"{self.iterations} iteration(s) · {', '.join(models) or self.model}"
        )
        if self.provider_errors:
            failed = ", ".join(f"{n} {name}" for name, n in sorted(self.provider_errors.items()))
            line += f" · {failed} · {self.fallback_calls} answered by the fallback"
        return line


def fallback_calls(usage: Usage, configured_model: str) -> int:
    """Calls answered by a model other than the configured primary.

    The primary is configured as `provider:name` while `response_metadata` reports the bare
    name, so a model counts as the primary when its name is part of the configured string.
    """
    return sum(
        n
        for v in usage.values()
        for name, n in v.get("models", {}).items()
        if name not in configured_model
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    repo TEXT, task TEXT, model TEXT, status TEXT,
    iterations INTEGER, steps INTEGER, wall_seconds REAL,
    tests_passed INTEGER, test_command TEXT,
    input_tokens INTEGER, output_tokens INTEGER,
    usage_json TEXT, tags_json TEXT
);
CREATE TABLE IF NOT EXISTS node_usage (
    run_id TEXT NOT NULL, node TEXT NOT NULL,
    calls INTEGER, input_tokens INTEGER, output_tokens INTEGER, models_json TEXT,
    PRIMARY KEY (run_id, node)
);
"""


# Columns added after the first release, applied with ALTER TABLE to ledgers that predate them.
# SQLite has no "ADD COLUMN IF NOT EXISTS", so the existing columns are read first.
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("runs", "provider_errors_json", "TEXT"),
)

_RUN_COLUMNS = (
    "run_id", "started_at", "repo", "task", "model", "status", "iterations", "steps",
    "wall_seconds", "tests_passed", "test_command", "input_tokens", "output_tokens",
    "usage_json", "tags_json", "provider_errors_json",
)


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            for table, column, kind in _MIGRATIONS:
                present = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
                if column not in present:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, rec: RunRecord) -> None:
        _, i, o = totals(rec.usage)
        with self._conn() as c:
            c.execute(
                f"INSERT OR REPLACE INTO runs ({', '.join(_RUN_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(_RUN_COLUMNS))})",
                (
                    rec.run_id, rec.started_at, rec.repo, rec.task, rec.model, rec.status,
                    rec.iterations, rec.steps, rec.wall_seconds,
                    None if rec.tests_passed is None else int(rec.tests_passed), rec.test_command,
                    i, o, json.dumps(rec.usage), json.dumps(rec.tags),
                    json.dumps(rec.provider_errors),
                ),
            )
            c.executemany(
                "INSERT OR REPLACE INTO node_usage VALUES (?,?,?,?,?,?)",
                [
                    (rec.run_id, node, v.get("calls", 0), v.get("input_tokens", 0),
                     v.get("output_tokens", 0), json.dumps(v.get("models", {})))
                    for node, v in rec.usage.items()
                ],
            )

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def usage_for(self, **tags: Any) -> Usage | None:
        """Per-node usage of the most recent run whose tags contain all of `tags`.

        The figure script uses it to recover node-level tokens for results files written before
        they stored `usage` themselves; new files carry it and never need the ledger.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT usage_json, tags_json FROM runs ORDER BY started_at DESC"
            ).fetchall()
        for row in rows:
            stored = json.loads(row["tags_json"] or "{}")
            if all(stored.get(k) == v for k, v in tags.items()):
                return json.loads(row["usage_json"] or "{}")
        return None

    def summary(self) -> dict[str, Any]:
        """Aggregates for `coder stats`: counts by status, mean tokens and time, per-node tokens."""
        with self._conn() as c:
            by_status = {
                r["status"]: r["n"]
                for r in c.execute("SELECT status, COUNT(*) n FROM runs GROUP BY status")
            }
            agg = c.execute(
                "SELECT COUNT(*) n, AVG(input_tokens + output_tokens) tokens, AVG(wall_seconds) secs,"
                " AVG(iterations) iters FROM runs"
            ).fetchone()
            per_node = [
                dict(r)
                for r in c.execute(
                    "SELECT node, SUM(calls) calls, SUM(input_tokens) input_tokens,"
                    " SUM(output_tokens) output_tokens FROM node_usage GROUP BY node ORDER BY node"
                )
            ]
            # Provider health is derived per run in Python: whether the fallback answered
            # depends on the run's own configured model, which SQL cannot compare in JSON.
            fallback_runs = 0
            provider_errors: dict[str, int] = {}
            for row in c.execute("SELECT model, usage_json, provider_errors_json FROM runs"):
                if fallback_calls(json.loads(row["usage_json"] or "{}"), row["model"] or ""):
                    fallback_runs += 1
                for name, count in json.loads(row["provider_errors_json"] or "{}").items():
                    provider_errors[name] = provider_errors.get(name, 0) + count
        n = agg["n"] or 0
        return {
            "runs": n,
            "by_status": by_status,
            "pass_rate": (by_status.get("passed", 0) / n) if n else None,
            "avg_tokens": agg["tokens"] or 0,
            "avg_seconds": agg["secs"] or 0,
            "avg_iterations": agg["iters"] or 0,
            "per_node": per_node,
            "fallback_runs": fallback_runs,
            "provider_errors": provider_errors,
        }
