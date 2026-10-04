"""Prompt templates are files named by version; the version is lineage on every Request row."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cairn.ingestion.filesystem import display_path
from cairn.serving.retrieval import Hit

PROMPTS_DIR = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class PromptTemplate:
    version: str
    text: str

    @classmethod
    def load(cls, version: str) -> PromptTemplate:
        path = PROMPTS_DIR / f"{version}.md"
        if not path.is_file():
            known = ", ".join(sorted(p.stem for p in PROMPTS_DIR.glob("*.md")))
            raise FileNotFoundError(f"no prompt template {version!r}; known: {known}")
        return cls(version=version, text=path.read_text(encoding="utf-8"))

    def render(self, context: str, question: str) -> str:
        return self.text.replace("{context}", context).replace("{question}", question)


@dataclass(frozen=True)
class ContextChunk:
    rank: int
    chunk_id: str
    title: str  # the document, as a path within its source
    heading_path: str
    text: str
    token_count: int


@dataclass(frozen=True)
class Context:
    chunks: tuple[ContextChunk, ...]
    prompt: str  # the fully rendered prompt an LLM generator sends
    template_version: str


def assemble(hits: list[Hit], template: PromptTemplate, question: str) -> Context:
    """Turn retrieved chunks into the context the generator sees."""
    chunks = tuple(
        ContextChunk(
            rank=h.rank,
            chunk_id=h.chunk_id,
            title=display_path(h.uri),
            heading_path=h.metadata.get("heading_path", ""),
            text=h.text,
            token_count=h.token_count,
        )
        for h in hits
    )
    rendered = "\n\n".join(
        f"[{c.rank}] {c.title}" + (f" › {c.heading_path}" if c.heading_path else "") + f"\n{c.text}"
        for c in chunks
    )
    return Context(
        chunks=chunks, prompt=template.render(rendered, question), template_version=template.version
    )
