"""The generated artefacts are valid in their target systems and match what is committed."""

from __future__ import annotations

import json
from pathlib import Path

import fastavro
import pytest
import sqlglot
from jsonschema import Draft202012Validator

from cairn_schemas.generate import avro, iceberg, jsonschema, postgres
from cairn_schemas.generate.__main__ import check, render_all
from cairn_schemas.models import ALL_MODELS, Chunk, Document, Embedding, Request

GENERATED = Path(__file__).resolve().parents[1] / "generated"


def test_committed_files_match_the_models():
    """The CI drift check: `make schemas` must have been run after the last model change."""
    assert check(GENERATED) == []


def test_render_is_deterministic():
    assert render_all() == render_all()


@pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: m.__name__)
def test_avro_schema_parses(model):
    parsed = fastavro.parse_schema(avro.avro_schema(model))
    assert parsed["name"] == f"cairn.{model.__name__}"


@pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: m.__name__)
def test_json_schema_is_valid_draft_2020_12(model):
    schema = json.loads(jsonschema.render(model))
    Draft202012Validator.check_schema(schema)
    assert schema["title"] == model.__name__


@pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: m.__name__)
def test_iceberg_ddl_parses_as_spark_sql(model):
    ddl = iceberg.render(model)
    (statement,) = sqlglot.parse(ddl, read="spark")
    assert statement is not None
    assert f"cairn.{model.TABLE}" in ddl
    assert "USING iceberg" in ddl
    for key in ("cairn.primary-key", "cairn.lineage-fields", "cairn.schema-version"):
        assert key in ddl


def test_type_overrides_reach_every_target():
    """Vectors are float32 in storage even though Python has only float."""
    assert "vector ARRAY<FLOAT>" in iceberg.render(Embedding)
    fields = {f["name"]: f for f in avro.avro_schema(Embedding)["fields"]}
    assert fields["vector"]["type"] == {"type": "array", "items": "float"}
    assert "vector REAL[] NOT NULL" in postgres.render(Embedding)


@pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: m.__name__)
def test_postgres_ddl_parses(model):
    """The fourth target (Stage 0, ADR-0003): one CREATE TABLE plus comments per model."""
    ddl = postgres.render(model)
    statements = sqlglot.parse(ddl, read="postgres")
    assert statements and statements[0] is not None
    assert f"CREATE TABLE IF NOT EXISTS {model.TABLE} (" in ddl
    assert f"PRIMARY KEY ({', '.join(model.PRIMARY_KEY)})" in ddl
    assert f"COMMENT ON TABLE {model.TABLE} IS" in ddl


def test_postgres_type_mapping():
    """Scalars map to native types; maps and nested records become JSONB; enums get a CHECK."""
    ddl = postgres.render(Request)
    assert "received_at TIMESTAMPTZ NOT NULL" in ddl
    assert "query_text TEXT," in ddl  # nullable: no NOT NULL
    assert "filters JSONB NOT NULL DEFAULT '{}'::JSONB" in ddl
    assert "retrieved_chunks JSONB NOT NULL DEFAULT '[]'::JSONB" in ddl
    assert "llm_params JSONB NOT NULL" in ddl
    assert "cost_usd DOUBLE PRECISION NOT NULL" in ddl
    assert "tokens_out BIGINT NOT NULL DEFAULT 0" in ddl
    assert "status TEXT NOT NULL CHECK (status IN ('ok', 'error', 'timeout'))" in ddl
    doc_ddl = postgres.render(Document)
    assert "acl TEXT[] NOT NULL DEFAULT '{}'::TEXT[]" in doc_ddl
    assert (
        "status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted'))" in doc_ddl
    )
    assert "-- Iceberg partition spec, not applied here: source_id, days(fetched_at)." in doc_ddl


def test_postgres_spine_script_has_every_table_in_order():
    script = postgres.render_all_in_spine_order(ALL_MODELS)
    positions = [script.index(f"CREATE TABLE IF NOT EXISTS {m.TABLE} (") for m in ALL_MODELS]
    assert positions == sorted(positions)


def test_nullable_and_default_handling():
    fields = {f["name"]: f for f in avro.avro_schema(Request)["fields"]}
    assert fields["query_text"]["type"] == ["null", "string"]
    assert fields["query_text"]["default"] is None
    assert fields["filters"]["default"] == {}
    assert fields["status"]["type"]["type"] == "enum"
    ddl = iceberg.render(Request)
    assert "query_text STRING COMMENT" in ddl
    assert "request_id STRING NOT NULL" in ddl


def test_column_order_puts_cross_cutting_fields_first_and_cost_last():
    ddl_lines = [line.strip().split(" ")[0] for line in iceberg.render(Embedding).splitlines()[2:]]
    columns = [c for c in ddl_lines if c and c[0].islower() and c not in ("ARRAY", "USING")]
    assert columns[:3] == ["tenant_id", "created_at", "schema_version"]
    assert columns[-3:] == ["tokens_in", "tokens_out", "cost_usd"]


def test_partition_specs_are_emitted():
    assert "PARTITIONED BY (chunker_version, bucket(128, doc_id))" in iceberg.render(Chunk)
