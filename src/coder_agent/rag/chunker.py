"""Language-aware chunking: split a file along its syntax, not every N lines.

A retriever returns chunks, and the model reads them. A chunk that starts in the middle of one
function and ends in the middle of the next is hard to embed (two topics, one vector) and hard
to read. Tree-sitter gives us the real boundaries: one chunk per top-level function or class,
methods split out of classes that are too big, imports and constants grouped as the module
preamble. Anything without a grammar we know (Markdown, YAML, an unknown extension) falls back to
overlapping line windows, and so does any single definition longer than the chunk budget.

Every chunk keeps its path, line range and symbol name, so a retrieved chunk can be cited as
`src/app.py:42-67 (Foo.method)` and the agent can open exactly that range with `read_file`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING

from coder_agent.config import settings
from coder_agent.rag.loader import RepoFile

if TYPE_CHECKING:
    from tree_sitter import Node, Parser


@dataclass(frozen=True)
class Chunk:
    """A contiguous slice of one file. Lines are 1-based and inclusive, like editors show them."""

    path: str
    language: str | None
    start_line: int
    end_line: int
    text: str
    kind: str  # function | class | type | variable | module | window
    symbol: str | None = None  # "Foo.method", "main", None for preambles and windows

    @property
    def id(self) -> str:
        """Stable across runs for identical content at the same place; changes when either does."""
        key = f"{self.path}:{self.start_line}:{self.end_line}:{self.text}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    @property
    def location(self) -> str:
        where = f"{self.path}:{self.start_line}-{self.end_line}"
        return f"{where} ({self.symbol})" if self.symbol else where


# ---------------------------------------------------------------------------
# Grammar tables
# ---------------------------------------------------------------------------

# node type -> chunk kind, per language. A node listed here becomes its own chunk.
DEFINITIONS: dict[str, dict[str, str]] = {
    "python": {"function_definition": "function", "class_definition": "class"},
    "javascript": {
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "function",
        "lexical_declaration": "variable",
        "variable_declaration": "variable",
    },
    "typescript": {
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "class_declaration": "class",
        "abstract_class_declaration": "class",
        "method_definition": "function",
        "method_signature": "function",
        "interface_declaration": "type",
        "type_alias_declaration": "type",
        "enum_declaration": "type",
        "lexical_declaration": "variable",
        "variable_declaration": "variable",
    },
    "go": {
        "function_declaration": "function",
        "method_declaration": "function",
        "type_declaration": "type",
    },
    "rust": {
        "function_item": "function",
        "function_signature_item": "function",
        "struct_item": "type",
        "enum_item": "type",
        "trait_item": "class",
        "impl_item": "class",
        "mod_item": "module",
    },
    "java": {
        "class_declaration": "class",
        "interface_declaration": "class",
        "enum_declaration": "type",
        "record_declaration": "type",
        "method_declaration": "function",
        "constructor_declaration": "function",
    },
    "ruby": {
        "method": "function",
        "singleton_method": "function",
        "class": "class",
        "module": "class",
    },
    "c": {"function_definition": "function", "struct_specifier": "type", "enum_specifier": "type"},
    "cpp": {
        "function_definition": "function",
        "class_specifier": "class",
        "struct_specifier": "type",
        "namespace_definition": "module",
    },
    "c_sharp": {
        "class_declaration": "class",
        "interface_declaration": "class",
        "struct_declaration": "type",
        "method_declaration": "function",
        "constructor_declaration": "function",
    },
}
DEFINITIONS["tsx"] = DEFINITIONS["typescript"]

# Wrapper nodes whose *child* is the real definition: keep the wrapper's span (the decorator or
# `export` keyword belongs with the function) but classify by the inner node's field.
WRAPPERS: dict[str, dict[str, str]] = {
    "python": {"decorated_definition": "definition"},
    "javascript": {"export_statement": "declaration"},
    "typescript": {"export_statement": "declaration"},
    "tsx": {"export_statement": "declaration"},
}

# Kinds whose body we descend into when the whole thing is too big for one chunk.
CONTAINER_KINDS = frozenset({"class", "module"})

# Where a container keeps its members. Fields first, then unnamed children by type.
_BODY_FIELDS = ("body", "declaration_list", "class_body")
_BODY_TYPES = (
    "class_body",
    "declaration_list",
    "block",
    "field_declaration_list",
    "body_statement",
)

_NAME_TYPES = (
    "identifier",
    "type_identifier",
    "property_identifier",
    "field_identifier",
    "constant",
)

# JS/TS `const f = () => ...` is a function in all but syntax; a plain `const x = 5` is noise.
_FUNCTION_VALUE_TYPES = frozenset(
    {"arrow_function", "function_expression", "function", "generator_function"}
)


@cache
def _parser(language: str) -> Parser | None:
    try:
        from tree_sitter_language_pack import Error, get_parser
    except ImportError:
        return None
    try:
        return get_parser(language)  # type: ignore[arg-type]
    except (Error, LookupError, OSError):  # unknown language name or missing grammar binary
        return None


def supports_syntax_chunking(language: str | None) -> bool:
    return language is not None and language in DEFINITIONS and _parser(language) is not None


# ---------------------------------------------------------------------------
# Syntax walk
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Segment:
    start_row: int  # 0-based, inclusive
    end_row: int
    kind: str
    symbol: str | None
    node: Node  # kept so containers can be split further


def _body_of(node: Node) -> Node | None:
    for field in _BODY_FIELDS:
        body = node.child_by_field_name(field)
        if body is not None:
            return body
    for child in node.children:
        if child.type in _BODY_TYPES:
            return child
    return None


def _first_descendant(node: Node, node_type: str, *, stop_at: Node | None) -> Node | None:
    stack = list(reversed(node.children))
    while stack:
        cur = stack.pop()
        if stop_at is not None and cur.id == stop_at.id:
            continue
        if cur.type == node_type:
            return cur
        stack.extend(reversed(cur.children))
    return None


def _text_of(node: Node | None) -> str | None:
    if node is None or node.text is None:
        return None
    return node.text.decode("utf-8", errors="replace")


def _node_name(node: Node) -> str | None:
    name = _text_of(node.child_by_field_name("name"))
    if name:
        return name
    if node.type == "impl_item":  # rust: `impl Trait for Type` is named after the type
        name = _text_of(node.child_by_field_name("type"))
        if name:
            return name
    # First identifier-like token outside the body: C declarators, Go `type T struct`,
    # JS `const f = ...`.
    body = _body_of(node)
    for name_type in _NAME_TYPES:
        name = _text_of(_first_descendant(node, name_type, stop_at=body))
        if name:
            return name
    return None


def _is_function_variable(node: Node) -> bool:
    for declarator in node.children:
        if declarator.type != "variable_declarator":
            continue
        value = declarator.child_by_field_name("value")
        if value is not None and value.type in _FUNCTION_VALUE_TYPES:
            return True
    return False


def _classify(node: Node, language: str) -> tuple[Node, str] | None:
    """Return (definition node, kind) if `node` is or wraps a definition, else None."""
    inner = node
    wrapper_field = WRAPPERS.get(language, {}).get(node.type)
    if wrapper_field:
        candidate = node.child_by_field_name(wrapper_field)
        if candidate is None:
            return None
        inner = candidate
    kind = DEFINITIONS[language].get(inner.type)
    if kind is None:
        return None
    if kind == "variable" and not _is_function_variable(inner):
        return None
    return inner, kind


def _iter_definitions(parent: Node, language: str, *, prefix: str) -> Iterator[_Segment]:
    """Definitions directly under `parent`, with leading Rust attributes attached to their item."""
    pending_start: int | None = None
    for child in parent.children:
        if language == "rust" and child.type == "attribute_item":
            if pending_start is None:
                pending_start = child.start_point[0]
            continue
        classified = _classify(child, language)
        if classified is None:
            pending_start = None
            continue
        inner, kind = classified
        name = _node_name(inner)
        symbol = f"{prefix}{name}" if name else (prefix.rstrip(".") or None)
        start = child.start_point[0] if pending_start is None else pending_start
        pending_start = None
        yield _Segment(start, child.end_point[0], kind, symbol, inner)


def window_rows(
    lengths: list[int], start: int, end: int, *, window: int, overlap: int, max_chars: int
) -> Iterator[tuple[int, int]]:
    """Split rows [start, end] into windows of at most `window` rows and `max_chars` characters.

    Both caps matter: sixty lines of Python is about a chunk, sixty lines of prose is four. A
    window always holds at least one row so a single very long line still gets indexed.
    """
    step_back = max(0, overlap)
    row = start
    while True:
        stop = row
        chars = lengths[row]
        while stop < end and stop - row + 1 < window and chars + 1 + lengths[stop + 1] <= max_chars:
            stop += 1
            chars += 1 + lengths[stop]
        yield row, stop
        if stop >= end:
            return
        # Overlap by a few rows for context, but never so far that we fail to advance.
        row = max(row + 1, stop + 1 - step_back)


class _Chunker:
    def __init__(self, file: RepoFile, *, max_chars: int, window: int, overlap: int) -> None:
        self.file = file
        self.lines = file.text.splitlines()
        self.lengths = [len(line) for line in self.lines]
        self.max_chars = max_chars
        self.window = window
        self.overlap = overlap

    def _text(self, start: int, end: int) -> str:
        return "\n".join(self.lines[start : end + 1])

    def _fits(self, start: int, end: int) -> bool:
        return len(self._text(start, end)) <= self.max_chars

    def _chunk(self, start: int, end: int, kind: str, symbol: str | None) -> Chunk:
        return Chunk(
            path=self.file.path,
            language=self.file.language,
            start_line=start + 1,
            end_line=end + 1,
            text=self._text(start, end),
            kind=kind,
            symbol=symbol,
        )

    def _windows(self, start: int, end: int, kind: str, symbol: str | None) -> Iterator[Chunk]:
        rows = window_rows(
            self.lengths,
            start,
            end,
            window=self.window,
            overlap=self.overlap,
            max_chars=self.max_chars,
        )
        for a, b in rows:
            yield self._chunk(a, b, kind, symbol)

    def by_windows(self) -> Iterator[Chunk]:
        if self.lines:
            yield from self._windows(0, len(self.lines) - 1, "window", None)

    def by_syntax(self, root: Node, language: str) -> Iterator[Chunk]:
        yield from self._members(root, language, prefix="", first=0, last=len(self.lines) - 1)

    def _members(
        self, parent: Node, language: str, *, prefix: str, first: int, last: int
    ) -> Iterator[Chunk]:
        """Definitions under `parent` as chunks; the text between them grouped as `module` filler."""
        cursor = first
        for seg in _iter_definitions(parent, language, prefix=prefix):
            if seg.start_row > cursor:
                yield from self._filler(cursor, seg.start_row - 1, "module", prefix)
            yield from self._definition(seg, language)
            cursor = seg.end_row + 1
        if cursor <= last:
            yield from self._filler(cursor, last, "module", prefix)

    def _filler(self, start: int, end: int, kind: str, prefix: str) -> Iterator[Chunk]:
        """Imports, constants, comments: dropped if blank, one chunk if small, windows if not."""
        if not self._text(start, end).strip():
            return
        symbol = prefix.rstrip(".") or None
        if self._fits(start, end):
            yield self._chunk(start, end, kind, symbol)
        else:
            yield from self._windows(start, end, kind, symbol)

    def _definition(self, seg: _Segment, language: str) -> Iterator[Chunk]:
        if self._fits(seg.start_row, seg.end_row):
            yield self._chunk(seg.start_row, seg.end_row, seg.kind, seg.symbol)
            return
        body = _body_of(seg.node)
        if seg.kind in CONTAINER_KINDS and body is not None and body.children:
            # Too big as a whole: the header (signature, docstring, class attributes) becomes the
            # container chunk and each member becomes its own chunk named `Class.member`.
            prefix = f"{seg.symbol}." if seg.symbol else ""
            body_row = body.start_point[0]
            if body_row > seg.start_row:
                yield from self._filler(seg.start_row, body_row - 1, seg.kind, prefix)
            yield from self._members(
                body, language, prefix=prefix, first=max(body_row, seg.start_row), last=seg.end_row
            )
            return
        yield from self._windows(seg.start_row, seg.end_row, seg.kind, seg.symbol)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def chunk_file(
    file: RepoFile,
    *,
    max_chars: int | None = None,
    window_lines: int | None = None,
    overlap_lines: int | None = None,
) -> list[Chunk]:
    """Chunk one file: by syntax when we have a grammar for it, by line windows otherwise."""
    chunker = _Chunker(
        file,
        max_chars=max_chars or settings.chunk_max_chars,
        window=window_lines or settings.chunk_window_lines,
        overlap=overlap_lines if overlap_lines is not None else settings.chunk_overlap_lines,
    )
    language = file.language
    parser = _parser(language) if language is not None and language in DEFINITIONS else None
    if parser is None or language is None:
        return list(chunker.by_windows())
    tree = parser.parse(file.text.encode("utf-8"))
    chunks = list(chunker.by_syntax(tree.root_node, language))
    # A grammar that recognised nothing (a file that is one expression, say) must still be
    # searchable.
    return chunks or list(chunker.by_windows())


def chunk_repo(files: Iterable[RepoFile], **kwargs: int | None) -> list[Chunk]:
    return [chunk for file in files for chunk in chunk_file(file, **kwargs)]
