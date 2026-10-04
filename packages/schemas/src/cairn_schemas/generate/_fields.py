"""Walk a Pydantic model into a small, generator-neutral description of its fields.

The four generators (Avro, Iceberg DDL, JSON Schema, Postgres DDL) all read this so the mapping from
Python types to storage types lives in exactly one place.
"""

from __future__ import annotations

import types
import typing
from dataclasses import dataclass, field
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from cairn_schemas.base import Costed, Row

Kind = Literal["string", "long", "double", "boolean", "timestamp", "array", "map", "enum", "record"]

_DATETIME_NAMES = {"datetime", "AwareDatetime", "NaiveDatetime", "PastDatetime", "FutureDatetime"}


@dataclass(frozen=True)
class TypeNode:
    kind: Kind
    item: TypeNode | None = None  # array items / map values
    symbols: tuple[str, ...] = ()  # enum
    enum_name: str = ""  # enum
    record: type[BaseModel] | None = None  # record


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: TypeNode
    nullable: bool
    description: str
    has_default: bool
    default: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


class UnsupportedType(TypeError):
    pass


def _is_datetime(t: Any) -> bool:
    return getattr(t, "__name__", None) in _DATETIME_NAMES


def _enum_name(owner: str, field_name: str) -> str:
    return owner + "".join(part.capitalize() for part in field_name.split("_"))


def resolve(annotation: Any, *, owner: str, field_name: str) -> tuple[TypeNode, bool]:
    """Map a field annotation to a TypeNode and a nullable flag."""
    origin = get_origin(annotation)

    if origin is typing.Annotated:  # e.g. ModelRef | None keeps the Annotated inside the union
        return resolve(get_args(annotation)[0], owner=owner, field_name=field_name)

    if origin in (typing.Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        nullable = len(args) != len(get_args(annotation))
        if len(args) != 1:
            raise UnsupportedType(f"{owner}.{field_name}: only Optional[T] unions are supported")
        node, inner_nullable = resolve(args[0], owner=owner, field_name=field_name)
        return node, nullable or inner_nullable

    if origin is Literal:
        symbols = get_args(annotation)
        if not all(isinstance(s, str) for s in symbols):
            raise UnsupportedType(f"{owner}.{field_name}: Literal symbols must be strings")
        return TypeNode(
            "enum", symbols=tuple(symbols), enum_name=_enum_name(owner, field_name)
        ), False

    if origin is list:
        (item_t,) = get_args(annotation)
        item, _ = resolve(item_t, owner=owner, field_name=field_name)
        return TypeNode("array", item=item), False

    if origin is dict:
        key_t, value_t = get_args(annotation)
        if key_t is not str:
            raise UnsupportedType(f"{owner}.{field_name}: map keys must be str")
        value, _ = resolve(value_t, owner=owner, field_name=field_name)
        return TypeNode("map", item=value), False

    if annotation is str:
        return TypeNode("string"), False
    if annotation is bool:  # before int: bool is a subclass of int
        return TypeNode("boolean"), False
    if annotation is int:
        return TypeNode("long"), False
    if annotation is float:
        return TypeNode("double"), False
    if _is_datetime(annotation):
        return TypeNode("timestamp"), False
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return TypeNode("record", record=annotation), False

    raise UnsupportedType(f"{owner}.{field_name}: unsupported annotation {annotation!r}")


def _default_of(info: FieldInfo) -> tuple[bool, Any]:
    if info.default is not PydanticUndefined:
        return True, info.default
    if info.default_factory is not None:
        value = info.default_factory()  # type: ignore[call-arg]
        if value in ([], {}):  # only empty containers are safe to inline in a schema
            return True, value
    return False, None


def ordered_field_names(model: type[BaseModel]) -> list[str]:
    """Row fields first, then the model's own fields, then cost fields last."""
    names = list(model.model_fields)
    if not issubclass(model, Row):
        return names
    base = [n for n in names if n in Row.model_fields]
    cost = [n for n in names if n in Costed.model_fields] if issubclass(model, Costed) else []
    own = [n for n in names if n not in base and n not in cost]
    return base + own + cost


def fields_of(model: type[BaseModel]) -> list[FieldSpec]:
    specs: list[FieldSpec] = []
    for name in ordered_field_names(model):
        info = model.model_fields[name]
        node, nullable = resolve(info.annotation, owner=model.__name__, field_name=name)
        has_default, default = _default_of(info)
        extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
        specs.append(
            FieldSpec(
                name=name,
                type=node,
                nullable=nullable,
                description=(info.description or "").strip(),
                has_default=has_default,
                default=default,
                extra=dict(extra),
            )
        )
    return specs


def table_metadata(model: type[Row]) -> dict[str, Any]:
    return {
        "table": model.TABLE,
        "model": model.__name__,
        "primary_key": list(model.PRIMARY_KEY),
        "lineage_fields": list(model.LINEAGE_FIELDS),
        "partition_by": list(model.PARTITION_BY),
        "schema_version": model.model_fields["schema_version"].default,
        "doc": (model.__doc__ or "").strip().splitlines()[0] if model.__doc__ else "",
    }
