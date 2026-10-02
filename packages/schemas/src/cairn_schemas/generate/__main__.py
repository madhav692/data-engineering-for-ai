"""Generate every contract artefact from the models, or check that the committed ones match.

python -m cairn_schemas.generate --out packages/schemas/generated          # write
python -m cairn_schemas.generate --out packages/schemas/generated --check  # CI drift check
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cairn_schemas.generate import avro, iceberg, jsonschema
from cairn_schemas.generate._fields import table_metadata
from cairn_schemas.models import ALL_MODELS


def render_all() -> dict[str, str]:
    """Relative path -> file content, for every artefact. Deterministic."""
    files: dict[str, str] = {}
    manifest = []
    for model in ALL_MODELS:
        files[f"avro/{model.TABLE}.avsc"] = avro.render(model)
        files[f"iceberg/{model.TABLE}.sql"] = iceberg.render(model)
        files[f"jsonschema/{model.__name__}.json"] = jsonschema.render(model)
        manifest.append(table_metadata(model))
    files["manifest.json"] = json.dumps(manifest, indent=2) + "\n"
    return files


def write(out: Path) -> list[str]:
    written = []
    for rel, content in render_all().items():
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(rel)
    return written


def check(out: Path) -> list[str]:
    """Paths that differ from the models, are missing, or are stale extras under out/."""
    expected = render_all()
    problems: list[str] = []
    for rel, content in expected.items():
        path = out / rel
        if not path.exists():
            problems.append(f"missing: {rel}")
        elif path.read_text(encoding="utf-8") != content:
            problems.append(f"differs: {rel}")
    for path in out.rglob("*"):
        if path.is_file() and path.suffix in {".avsc", ".sql", ".json"}:
            rel = path.relative_to(out).as_posix()
            if rel not in expected:
                problems.append(f"stale: {rel}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cairn_schemas.generate", description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="directory for generated files")
    parser.add_argument(
        "--check", action="store_true", help="verify instead of write; exit 1 on drift"
    )
    args = parser.parse_args(argv)

    if args.check:
        problems = check(args.out)
        if problems:
            print("generated schemas are out of date; run `make schemas`:", file=sys.stderr)
            for p in problems:
                print(f"  {p}", file=sys.stderr)
            return 1
        print(f"generated schemas match the models ({len(render_all())} files)")
        return 0

    for rel in write(args.out):
        print(f"wrote {args.out / rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
