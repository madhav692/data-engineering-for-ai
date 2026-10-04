"""Golden sets: questions with the documents that answer them.

A set is a JSONL file under ``datasets/golden/``; each line is
``{"question": "...", "expected": ["<source_id>/<path within the source>", ...]}``.
The expected documents are named by path, not by ``doc_id``, so the file is readable; the
runner derives the ids the same way the connector does. The set's version is the hash of the
file, so a changed question is a new version and runs on different versions are not compared.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from cairn_schemas import ids

from cairn.ingestion.filesystem import document_uri


@dataclass(frozen=True)
class GoldenQuestion:
    question: str
    expected: tuple[str, ...]  # "source_id/relative/path.md"

    def expected_doc_ids(self) -> set[str]:
        out = set()
        for ref in self.expected:
            source_id, _, relpath = ref.partition("/")
            if not relpath:
                raise ValueError(f"expected document must be '<source_id>/<path>': {ref!r}")
            out.add(ids.doc_id(source_id, document_uri(source_id, relpath)))
        return out


@dataclass(frozen=True)
class GoldenSet:
    set_id: str
    version: str
    questions: tuple[GoldenQuestion, ...]
    path: Path


def load_golden(path: Path, set_id: str | None = None) -> GoldenSet:
    data = path.read_bytes()
    questions = []
    for lineno, line in enumerate(data.decode("utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            obj = json.loads(line)
            questions.append(
                GoldenQuestion(question=obj["question"], expected=tuple(obj["expected"]))
            )
        except (KeyError, ValueError) as err:
            raise ValueError(f"{path}:{lineno}: {err}") from err
    if not questions:
        raise ValueError(f"{path}: no questions")
    return GoldenSet(
        set_id=set_id or path.stem,
        version=ids.sha256_hex(data)[:12],
        questions=tuple(questions),
        path=path,
    )
