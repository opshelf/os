"""Development-time OpenAPI checks; no dependency on a running service."""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from typing import Any, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from openapi_spec_validator import validate
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


def validate_document(document: dict[str, Any]) -> None:
    validate(cast(Mapping[Hashable, Any], document))
    tags = {tag["name"] for tag in document.get("tags", [])}
    operation_ids: set[str] = set()
    for path, item in document["paths"].items():
        for method, operation in item.items():
            if method not in METHODS:
                continue
            label = f"{method.upper()} {path}"
            assert operation.get("summary", "").strip(), f"Missing summary: {label}"
            description = operation.get("description", "").strip()
            assert description and description.rstrip(".") != operation["summary"].rstrip("."), (
                f"Missing guidance: {label}"
            )
            assert operation.get("tags") and set(operation["tags"]) <= tags, (
                f"Undocumented tag: {label}"
            )
            identifier = operation["operationId"]
            assert identifier not in operation_ids, f"Duplicate operation ID: {identifier}"
            operation_ids.add(identifier)
            for code, response in operation["responses"].items():
                if code.startswith("2") and code != "204":
                    assert response.get("content"), f"Missing response content: {label} {code}"
                    for media in response["content"].values():
                        schema = media.get("schema", {})
                        assert schema, f"Missing response schema: {label} {code}"
                        assert not (
                            schema.get("type") == "object"
                            and not schema.get("properties")
                            and not schema.get("$ref")
                        ), f"Unspecified response object: {label}"
    uri = "urn:opshelf:openapi"
    registry = Registry().with_resource(
        uri, Resource.from_contents(document, default_specification=DRAFT202012)
    )

    def walk(value: Any, pointer: str = "") -> None:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/"):
                target: Any = document
                for part in reference[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            # Schema-level examples, not the OpenAPI named media examples map.
            if isinstance(value.get("examples"), list):
                validator = Draft202012Validator({"$ref": uri + "#" + pointer}, registry=registry)
                for example in value["examples"]:
                    validator.validate(example)
            if "schema" in value and "example" in value:
                Draft202012Validator(
                    {"$ref": uri + "#" + pointer + "/schema"}, registry=registry
                ).validate(value["example"])
            for key, child in value.items():
                walk(child, pointer + "/" + key.replace("~", "~0").replace("/", "~1"))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, pointer + "/" + str(index))

    walk(document)
