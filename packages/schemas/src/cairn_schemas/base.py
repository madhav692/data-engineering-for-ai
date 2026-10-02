"""Base classes for every Cairn table row.

Three fields ride on every row; they are the cross-cutting properties from A2:

- ``tenant_id``   isolation and cost attribution
- ``created_at``  freshness: a timestamp at every hop, so lag can be computed between any two
- the version that made the row, declared per model in ``LINEAGE_FIELDS``

Rows that touched a model also carry tokens and cost (``Costed``), so every token is attributed.

Each concrete model also declares its contract metadata as class variables:
``TABLE``, ``PRIMARY_KEY``, ``LINEAGE_FIELDS`` and ``PARTITION_BY``. The generators read
them to emit Iceberg DDL and the manifest; the tests read them to prove the invariants.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class Row(BaseModel):
    """A row in a Cairn table. Immutable once constructed; unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(
        min_length=1,
        max_length=128,
        description="Tenant that owns the row. Every index query and cost report filters on it.",
    )
    created_at: AwareDatetime = Field(
        description=(
            "When this row was written (UTC, timezone-aware). Freshness is computed from it."
        )
    )
    schema_version: int = Field(
        default=1,
        ge=1,
        description=(
            "Version of this row's schema. Bumped on any field change; see CONTRIBUTING.md."
        ),
    )

    # Contract metadata, set by each concrete model.
    TABLE: ClassVar[str]
    PRIMARY_KEY: ClassVar[tuple[str, ...]]
    LINEAGE_FIELDS: ClassVar[tuple[str, ...]]
    PARTITION_BY: ClassVar[tuple[str, ...]] = ()

    def lineage(self) -> dict[str, Any]:
        """The versions that produced this row, by field name."""
        return {name: getattr(self, name) for name in self.LINEAGE_FIELDS}

    def key(self) -> tuple[Any, ...]:
        """The primary-key values, in ``PRIMARY_KEY`` order."""
        return tuple(getattr(self, name) for name in self.PRIMARY_KEY)


class Costed(BaseModel):
    """Mixin for rows that touched a model. Every token is attributed; cost is an SLI."""

    tokens_in: int = Field(ge=0, description="Input tokens charged for this row.")
    tokens_out: int = Field(default=0, ge=0, description="Output tokens charged for this row.")
    cost_usd: float = Field(
        ge=0, description="Cost in USD at the price in force when the row was written."
    )
