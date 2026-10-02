"""Avro schemas for the Kafka topics that carry Cairn rows (schema registry subjects)."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from cairn_schemas.generate._fields import FieldSpec, TypeNode, fields_of

NAMESPACE = "cairn"


def _avro_type(node: TypeNode, defined: set[str], extra: dict[str, Any]) -> Any:
    if "avro_type" in extra:
        return extra["avro_type"]
    match node.kind:
        case "string":
            return "string"
        case "long":
            return "long"
        case "double":
            return "double"
        case "boolean":
            return "boolean"
        case "timestamp":
            return {"type": "long", "logicalType": "timestamp-micros"}
        case "array":
            assert node.item is not None
            return {"type": "array", "items": _avro_type(node.item, defined, {})}
        case "map":
            assert node.item is not None
            return {"type": "map", "values": _avro_type(node.item, defined, {})}
        case "enum":
            if node.enum_name in defined:
                return node.enum_name
            defined.add(node.enum_name)
            return {"type": "enum", "name": node.enum_name, "symbols": list(node.symbols)}
        case "record":
            assert node.record is not None
            name = node.record.__name__
            if name in defined:
                return name
            defined.add(name)
            return _record(node.record, defined)
    raise AssertionError(node.kind)


def _field(spec: FieldSpec, defined: set[str]) -> dict[str, Any]:
    avro_t = _avro_type(spec.type, defined, spec.extra)
    out: dict[str, Any] = {"name": spec.name}
    if spec.nullable:
        out["type"] = ["null", avro_t]
        out["default"] = None
    else:
        out["type"] = avro_t
        if spec.has_default and spec.default is not None:
            out["default"] = spec.default
    if spec.description:
        out["doc"] = spec.description
    return out


def _record(model: type[BaseModel], defined: set[str]) -> dict[str, Any]:
    doc = (model.__doc__ or "").strip().splitlines()
    return {
        "type": "record",
        "name": model.__name__,
        "namespace": NAMESPACE,
        "doc": doc[0] if doc else "",
        "fields": [_field(spec, defined) for spec in fields_of(model)],
    }


def avro_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The Avro schema of one table row, with nested records and enums defined inline once."""
    return _record(model, defined={model.__name__})


def render(model: type[BaseModel]) -> str:
    return json.dumps(avro_schema(model), indent=2, sort_keys=False) + "\n"
