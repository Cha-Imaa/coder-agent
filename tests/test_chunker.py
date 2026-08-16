"""Chunker: syntax boundaries when we have a grammar, bounded line windows when we do not."""

from __future__ import annotations

import pytest

from coder_agent.rag import Chunk, RepoFile, chunk_file, supports_syntax_chunking
from coder_agent.rag.chunker import window_rows


def make_file(path: str, text: str) -> RepoFile:
    from coder_agent.rag.loader import detect_language, file_hash

    return RepoFile(
        path=path, text=text, sha256=file_hash(text.encode()), language=detect_language(path)
    )


def lines_of(chunk: Chunk, text: str) -> str:
    """The chunk text must be exactly the file's lines start..end; this rebuilds it to compare."""
    return "\n".join(text.splitlines()[chunk.start_line - 1 : chunk.end_line])


PYTHON_SRC = '''"""Module docstring."""

import os
from pathlib import Path

LIMIT = 10


def top(a, b):
    """Add."""
    return a + b


class Foo:
    """A class."""

    attr = 1

    def method(self, x):
        return x * 2

    @property
    def prop(self):
        return self.attr


@decorator
async def later():
    pass
'''


def test_python_splits_at_top_level_definitions() -> None:
    chunks = chunk_file(make_file("pkg/mod.py", PYTHON_SRC), max_chars=10_000)
    kinds = [(c.kind, c.symbol) for c in chunks]
    assert kinds == [
        ("module", None),  # docstring, imports, LIMIT
        ("function", "top"),
        ("class", "Foo"),
        ("function", "later"),
    ]
    later = chunks[-1]
    assert later.text.startswith("@decorator")  # decorator travels with its function
    assert later.text.rstrip().endswith("pass")
    for c in chunks:
        assert c.text == lines_of(c, PYTHON_SRC)
        assert c.path == "pkg/mod.py" and c.language == "python"


def test_oversized_class_is_split_into_header_and_methods() -> None:
    chunks = chunk_file(make_file("mod.py", PYTHON_SRC), max_chars=120)
    symbols = [(c.kind, c.symbol) for c in chunks if c.symbol and c.symbol.startswith("Foo")]
    assert ("class", "Foo") in symbols  # header: `class Foo:` + docstring + attr
    assert ("function", "Foo.method") in symbols
    assert ("function", "Foo.prop") in symbols
    header = next(c for c in chunks if c.symbol == "Foo" and c.kind == "class")
    assert "class Foo:" in header.text
    assert "def method" not in header.text
    prop = next(c for c in chunks if c.symbol == "Foo.prop")
    assert prop.text.lstrip().startswith("@property")


def test_oversized_function_falls_back_to_windows_but_keeps_its_name() -> None:
    body = "\n".join(f"    x{i} = {i}" for i in range(200))
    src = f"def big():\n{body}\n    return x1\n"
    chunks = chunk_file(make_file("big.py", src), max_chars=400, window_lines=30, overlap_lines=5)
    assert len(chunks) > 1
    assert all(c.kind == "function" and c.symbol == "big" for c in chunks)
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == len(src.splitlines())
    assert all(len(c.text) <= 400 for c in chunks)


def test_unknown_language_uses_line_windows() -> None:
    text = "\n".join(f"line {i}" for i in range(100))
    chunks = chunk_file(make_file("notes.txt", text), window_lines=40, overlap_lines=10)
    assert [c.kind for c in chunks] == ["window"] * len(chunks)
    assert chunks[0].start_line == 1 and chunks[0].end_line == 40
    assert chunks[1].start_line == 31  # 40 - 10 overlap, 1-based
    assert chunks[-1].end_line == 100
    for c in chunks:
        assert c.text == lines_of(c, text)


def test_windows_respect_the_character_budget_for_prose() -> None:
    long_lines = "\n".join("word " * 40 for _ in range(30))  # 200 chars per line
    chunks = chunk_file(make_file("README.md", long_lines), max_chars=1000, window_lines=60)
    assert len(chunks) > 1
    assert all(len(c.text) <= 1000 for c in chunks)
    assert chunks[-1].end_line == 30


def test_window_rows_always_advances_and_covers_everything() -> None:
    lengths = [5000] * 3 + [10] * 5  # single lines over budget must still be emitted alone
    rows = list(window_rows(lengths, 0, 7, window=4, overlap=2, max_chars=100))
    assert rows[0] == (0, 0)
    assert rows[1] == (1, 1)
    assert rows[-1][1] == 7
    covered = set()
    for a, b in rows:
        covered.update(range(a, b + 1))
    assert covered == set(range(8))


def test_empty_file_yields_no_chunks() -> None:
    assert chunk_file(make_file("empty.py", "")) == []
    assert chunk_file(make_file("empty.md", "")) == []


def test_blank_gaps_between_definitions_are_not_chunks() -> None:
    src = "def a():\n    pass\n\n\n\ndef b():\n    pass\n"
    chunks = chunk_file(make_file("m.py", src))
    assert [c.symbol for c in chunks] == ["a", "b"]


JS_SRC = """import x from "y";

export function named() { return 1; }

const arrow = (a) => a + 1;

const NOT_A_FUNCTION = 42;

export default class Widget {
  render() { return null; }
}
"""


def test_javascript_exports_arrow_functions_and_classes() -> None:
    chunks = chunk_file(make_file("app.js", JS_SRC), max_chars=10_000)
    symbols = [(c.kind, c.symbol) for c in chunks]
    assert ("function", "named") in symbols
    assert ("variable", "arrow") in symbols
    assert ("class", "Widget") in symbols
    assert not any(s == "NOT_A_FUNCTION" for _, s in symbols)  # plain const is module filler
    named = next(c for c in chunks if c.symbol == "named")
    assert named.text.startswith("export function")


GO_SRC = """package main

import "fmt"

type Server struct{ port int }

func (s *Server) Start() { fmt.Println(s.port) }

func main() { (&Server{80}).Start() }
"""

RUST_SRC = """use std::fmt;

#[derive(Debug)]
pub struct Point { x: i32 }

impl Point {
    pub fn new(x: i32) -> Self { Point { x } }
}

fn main() { let _p = Point::new(1); }
"""


def test_go_names_types_functions_and_methods() -> None:
    chunks = chunk_file(make_file("main.go", GO_SRC), max_chars=10_000)
    assert [(c.kind, c.symbol) for c in chunks] == [
        ("module", None),
        ("type", "Server"),
        ("function", "Start"),
        ("function", "main"),
    ]


def test_rust_attaches_attributes_and_splits_impl_methods() -> None:
    chunks = chunk_file(make_file("lib.rs", RUST_SRC), max_chars=10_000)
    point = next(c for c in chunks if c.symbol == "Point" and c.kind == "type")
    assert point.text.startswith("#[derive(Debug)]")
    assert ("class", "Point") in [(c.kind, c.symbol) for c in chunks]  # the impl block
    small = chunk_file(make_file("lib.rs", RUST_SRC), max_chars=60)
    assert ("function", "Point.new") in [(c.kind, c.symbol) for c in small]


@pytest.mark.parametrize(
    "language", ["python", "javascript", "typescript", "tsx", "go", "rust", "java"]
)
def test_grammars_we_rely_on_are_installed(language: str) -> None:
    assert supports_syntax_chunking(language)


def test_unsupported_language_is_reported() -> None:
    assert not supports_syntax_chunking("markdown")
    assert not supports_syntax_chunking(None)


def test_chunk_id_is_stable_and_content_sensitive() -> None:
    a = chunk_file(make_file("m.py", "def f():\n    return 1\n"))[0]
    b = chunk_file(make_file("m.py", "def f():\n    return 1\n"))[0]
    c = chunk_file(make_file("m.py", "def f():\n    return 2\n"))[0]
    assert a.id == b.id
    assert a.id != c.id
    assert a.location == "m.py:1-2 (f)"
