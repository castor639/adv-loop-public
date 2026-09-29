"""A small draft-07 validator for the compiled model-output schemas.

Covers exactly the keywords harness.schema emits: type, properties, required,
additionalProperties, items, minItems, maxItems, enum, const, oneOf, anyOf,
minLength, pattern, minimum, maximum. Returns a list of problems as
"path: message" strings so a retry prompt can quote them.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(value: Any, schema: Dict[str, Any], path: str = "$") -> List[str]:
    problems: List[str] = []
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(TYPE_CHECKS[t](value) for t in types if t in TYPE_CHECKS):
            return [f"{path}: expected {' or '.join(types)}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: must be one of {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        problems.append(f"{path}: must equal {schema['const']!r}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append(f"{path}: shorter than {schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            problems.append(f"{path}: does not match {schema['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path}: above maximum {schema['maximum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{path}: missing required field '{key}'")
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                problems.extend(validate(item, props[key], f"{path}.{key}"))
            elif extra is False:
                problems.append(f"{path}: unknown field '{key}'")
            elif isinstance(extra, dict):
                problems.extend(validate(item, extra, f"{path}.{key}"))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append(f"{path}: needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{path}: allows at most {schema['maxItems']} items")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                problems.extend(validate(item, items, f"{path}[{index}]"))
    if "oneOf" in schema:
        branches = [validate(value, sub, path) for sub in schema["oneOf"]]
        matches = [i for i, found in enumerate(branches) if not found]
        if len(matches) > 1:
            problems.append(f"{path}: must match exactly one alternative (matched {len(matches)})")
        elif not matches:
            # Saying only "matched 0" gives the model nothing to fix, and it will fail the same way on
            # the retry. Report the alternative it meant: the one whose type it already got right, and
            # among those the one it came closest to satisfying.
            def _rank(found):
                wrong_type = any(item.startswith(f"{path}: expected ") for item in found)
                return (1 if wrong_type else 0, len(found))

            closest = min(branches, key=_rank)
            problems.append(f"{path}: matches no alternative; closest one reports: " + "; ".join(closest[:6]))
    if "anyOf" in schema:
        if not any(not validate(value, sub, path) for sub in schema["anyOf"]):
            problems.append(f"{path}: matches none of the alternatives")
    return problems
