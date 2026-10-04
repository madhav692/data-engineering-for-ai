"""``cairn migrate``: create the schema. Idempotent; the api container runs it at startup.

Order: the pgvector extension; the seven contract tables in lineage-spine order, rendered by
the Postgres generator from the models (``make check-schemas`` guarantees this is byte-for-
byte what is committed under ``packages/schemas/generated/postgres/``); then the platform's own
non-contract objects from ``migrations/*.sql``.
"""

from __future__ import annotations

from pathlib import Path

from cairn_schemas.generate import postgres
from cairn_schemas.models import ALL_MODELS

from cairn.lakehouse.db import Db

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def contract_ddl() -> str:
    return postgres.render_all_in_spine_order(ALL_MODELS)


def platform_ddl() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(MIGRATIONS_DIR.glob("*.sql")))


def migrate(db: Db, *, reset: bool = False) -> list[str]:
    """Apply the schema. ``reset=True`` drops everything first (the tests use it)."""
    applied: list[str] = []
    with db.transaction() as tx:
        if reset:
            tx.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
            applied.append("reset")
        tx.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        applied.append("extension vector")
        tx.execute(contract_ddl())
        applied.extend(f"table {m.TABLE}" for m in ALL_MODELS)
        tx.execute(platform_ddl())
        applied.extend(f"migration {p.name}" for p in sorted(MIGRATIONS_DIR.glob("*.sql")))
    return applied
