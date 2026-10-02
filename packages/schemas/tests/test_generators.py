"""The generated artefacts are valid in their target systems and match what is committed."""

from __future__ import annotations

import json
from pathlib import Path

import fastavro
import pytest
import sqlglot
from jsonschema import Draft202012Validator

from cairn_schemas.generate import avro, iceberg, jsonschema
from cairn_schemas.generate.__main__ import check, render_all
from cairn_schemas.models import ALL_MODELS, Chunk, Embedding, Request

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
