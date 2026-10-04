"""The Markdown parser and chunker, each a versioned pure function.

``parse`` (``md-raw-v1``) turns bytes into text: UTF-8, line endings normalised, YAML front
matter and HTML comments removed, nothing else. ``chunk_document`` (``md-struct-v1``) walks the
heading structure, keeps a heading path for every piece, and packs blocks (paragraphs, lists,
fenced code) under a heading until the next block would push the chunk over ``MAX_TOKENS``,
counted with the embedding model's own tokenizer. A block that is itself over budget is split
on lines, then sentences, then words.

What a chunk's identity is made of: ``Chunk.build`` derives ``chunk_id`` from the document's
identity, the two versions, the hash of the text and an occurrence counter, and from nothing
else. Position (ordinal, byte offsets) is recorded but is not identity, so editing one paragraph
leaves every other chunk's id unchanged. That is what lets derivation embed only what changed.

Both functions are deterministic: the same bytes and the same tokenizer always give the same
chunks. Changing either version string is a deliberate backfill (every chunk id changes).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from cairn_schemas import ids
from cairn_schemas.models import Chunk, Document

PARSER_VERSION = "md-raw-v1"
CHUNKER_VERSION = "md-struct-v1"
MAX_TOKENS = 400


def live_versions() -> dict[str, str]:
    """The parser and chunker versions in force, as query parameters. Read at call time."""
    return {"parser_version": PARSER_VERSION, "chunker_version": CHUNKER_VERSION}


CountTokens = Callable[[str], int]

_FRONT_MATTER = re.compile(r"\A---[ \t]*\n.*?\n(?:---|\.\.\.)[ \t]*\n", re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^(`{3,}|~{3,})")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def parse(data: bytes) -> str:
    """md-raw-v1: decode, normalise newlines, drop front matter and HTML comments."""
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    text = _FRONT_MATTER.sub("", text, count=1)
    text = _HTML_COMMENT.sub("", text)
    return text


@dataclass(frozen=True)
class Section:
    heading_path: tuple[str, ...]
    start: int  # character offset into the parsed text
    end: int
    body: str


@dataclass(frozen=True)
class _Block:
    kind: str  # "heading" | "text"
    start: int
    end: int
    level: int = 0
    title: str = ""


def _blocks(text: str) -> Iterator[_Block]:
    """Headings, and maximal runs of non-blank lines. A fenced code block is one block."""
    offset = 0
    run_start: int | None = None
    run_end = 0
    in_fence: str | None = None
    for line in text.split("\n"):
        line_end = offset + len(line)
        stripped = line.strip()
        if in_fence is None:
            fence = _FENCE.match(stripped)
            if fence:
                in_fence = fence.group(1)[0]
                if run_start is None:
                    run_start = offset
                run_end = line_end
            elif not stripped:
                if run_start is not None:
                    yield _Block("text", run_start, run_end)
                    run_start = None
            elif (heading := _HEADING.match(line)) and not line.startswith("    "):
                if run_start is not None:
                    yield _Block("text", run_start, run_end)
                    run_start = None
                yield _Block(
                    "heading", offset, line_end, level=len(heading.group(1)), title=heading.group(2)
                )
            else:
                if run_start is None:
                    run_start = offset
                run_end = line_end
        else:
            run_end = line_end
            if stripped.startswith(in_fence * 3):
                in_fence = None
        offset = line_end + 1
    if run_start is not None:
        yield _Block("text", run_start, run_end)


def _pieces(
    text: str, start: int, end: int, count: CountTokens, budget: int
) -> list[tuple[int, int]]:
    """Split text[start:end] into (start, end) pieces of at most ``budget`` tokens."""
    body = text[start:end]
    if count(body) <= budget:
        return [(start, end)]
    for splitter in (re.compile(r"\n"), _SENTENCE_END, re.compile(r"\s+")):
        parts = _split_spans(body, splitter)
        if len(parts) > 1 and all(count(body[a:b]) <= budget for a, b in parts):
            return _pack(body, parts, count, budget, start)
    # A single token longer than the budget: emit it whole rather than cut inside a word.
    return [(start, end)]


def _split_spans(body: str, splitter: re.Pattern[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    last = 0
    for match in splitter.finditer(body):
        if match.start() > last:
            spans.append((last, match.start()))
        last = match.end()
    if last < len(body):
        spans.append((last, len(body)))
    return spans


def _pack(
    body: str,
    parts: list[tuple[int, int]],
    count: CountTokens,
    budget: int,
    base: int,
) -> list[tuple[int, int]]:
    """Greedily join consecutive parts while the joined text stays within budget."""
    out: list[tuple[int, int]] = []
    cur_start, cur_end = parts[0]
    for a, b in parts[1:]:
        if count(body[cur_start:b]) <= budget:
            cur_end = b
        else:
            out.append((base + cur_start, base + cur_end))
            cur_start, cur_end = a, b
    out.append((base + cur_start, base + cur_end))
    return out


def sections(
    text: str, count_tokens: CountTokens, max_tokens: int = MAX_TOKENS
) -> Iterator[Section]:
    """Heading-aware sections of at most ``max_tokens`` tokens; heading-only runs are skipped."""
    path: list[tuple[int, str]] = []
    current: list[tuple[int, int]] = []  # (start, end) of the blocks in the open section
    current_tokens = 0
    has_content = False

    def flush() -> Iterator[Section]:
        nonlocal current, current_tokens, has_content
        if current and has_content:
            start, end = current[0][0], current[-1][1]
            body = text[start:end]
            lead = len(body) - len(body.lstrip())
            body = body.strip()
            if body:
                yield Section(
                    tuple(t for _, t in path), start + lead, start + lead + len(body), body
                )
        current, current_tokens, has_content = [], 0, False

    for block in _blocks(text):
        if block.kind == "heading":
            yield from flush()
            while path and path[-1][0] >= block.level:
                path.pop()
            path.append((block.level, block.title))
            current = [(block.start, block.end)]
            current_tokens = count_tokens(text[block.start : block.end])
            continue
        for start, end in _pieces(text, block.start, block.end, count_tokens, max_tokens):
            tokens = count_tokens(text[start:end])
            if current and current_tokens + tokens > max_tokens:
                yield from flush()
            current.append((start, end))
            current_tokens += tokens
            has_content = True
    yield from flush()


def chunk_document(doc: Document, text: str, count_tokens: CountTokens) -> list[Chunk]:
    """Cut one document version into chunks with content-addressed ids."""
    byte_offsets = _byte_offsets(text)
    chunks: list[Chunk] = []
    seen: dict[str, int] = {}
    cut = list(sections(text, count_tokens, MAX_TOKENS))
    if not cut and text.strip():
        # A heading-only document still becomes one chunk, so it is searchable and so the
        # backfill query does not see it as underived on every run. A blank file yields nothing.
        lead = len(text) - len(text.lstrip())
        body = text.strip()
        cut = [Section(_heading_only_path(text), lead, lead + len(body), body)]
    for ordinal, section in enumerate(cut):
        sha = ids.sha256_hex(section.body)
        occurrence = seen.get(sha, 0)
        seen[sha] = occurrence + 1
        chunks.append(
            Chunk.build(
                document=doc,
                parser_version=live_versions()["parser_version"],
                chunker_version=live_versions()["chunker_version"],
                ordinal=ordinal,
                occurrence=occurrence,
                byte_start=byte_offsets[section.start],
                byte_end=byte_offsets[section.end],
                text=section.body,
                token_count=max(1, count_tokens(section.body)),
                metadata={"heading_path": " > ".join(section.heading_path)},
            )
        )
    return chunks


def _heading_only_path(text: str) -> tuple[str, ...]:
    path: list[tuple[int, str]] = []
    for block in _blocks(text):
        if block.kind == "heading":
            while path and path[-1][0] >= block.level:
                path.pop()
            path.append((block.level, block.title))
    return tuple(title for _, title in path)


def _byte_offsets(text: str) -> list[int]:
    """UTF-8 byte offset of every character position (length n + 1)."""
    if text.isascii():
        return list(range(len(text) + 1))
    offsets = [0] * (len(text) + 1)
    total = 0
    for i, ch in enumerate(text):
        total += len(ch.encode("utf-8"))
        offsets[i + 1] = total
    return offsets
