"""JSON Schema helpers for structured output across vendors.

OpenAI's strict mode (used by Codex ``output_schema``) needs every object to list all of
its properties as required and set ``additionalProperties: false``, with no ``$ref`` or
``default``. Optional fields become nullable instead. Claude accepts the same schema.
"""

from __future__ import annotations

import copy
from typing import Any

DROP = {"default", "title", "minItems", "maxItems", "minimum", "maximum"}


def strict(schema: dict[str, Any]) -> dict[str, Any]:
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {})

    def walk(node: Any, optional: bool = False) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            node = copy.deepcopy(defs[node["$ref"].split("/")[-1]])
        node = {k: v for k, v in node.items() if k not in DROP}
        if node.get("type") == "object" or "properties" in node:
            props = node.get("properties", {})
            required = set(node.get("required", []))
            node["properties"] = {k: walk(v, optional=k not in required) for k, v in props.items()}
            node["required"] = list(props)
            node["additionalProperties"] = False
            node["type"] = "object"
        for key in ("items", "anyOf"):
            if key in node:
                node[key] = walk(node[key])
        if optional and "anyOf" not in node and isinstance(node.get("type"), str):
            node["type"] = [node["type"], "null"]
        return node

    return walk(schema)


def drop_nulls(value: Any) -> Any:
    """Strict schemas send optional fields as null; our models want them absent."""
    if isinstance(value, dict):
        return {k: drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [drop_nulls(v) for v in value]
    return value
