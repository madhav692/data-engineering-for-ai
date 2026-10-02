"""JSON Schema for the API surface, straight from Pydantic, dumped deterministically."""

from __future__ import annotations

import json

from pydantic import BaseModel


def render(model: type[BaseModel]) -> str:
    schema = model.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://cairn.dev/schemas/{model.__name__}.json"
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"
