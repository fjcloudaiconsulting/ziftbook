import json
from pathlib import Path
from typing import Any

from app.main import create_app, openapi_document

CONTRACT = Path(__file__).parent.parent / "openapi.json"


def schemas_reachable_from(spec: dict[str, Any], *starts: Any) -> set[str]:
    """Schema names reachable, transitively, from these nodes (a parameter, a requestBody, a
    responses map, or a whole operation): via `$ref` anywhere in the structure - `allOf`/`anyOf`/
    `oneOf`, `items`, `additionalProperties`, nested `content`/`schema`, or any other shape the
    document uses. Generic on purpose: a fence that only knew today's OpenAPI shapes would miss
    tomorrow's."""
    seen: set[str] = set()
    queue: list[Any] = list(starts)
    while queue:
        node = queue.pop()
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[-1]
                if name not in seen:
                    seen.add(name)
                    queue.append(spec["components"]["schemas"][name])
            else:
                queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return seen


def test_every_error_in_the_contract_is_a_code() -> None:
    # The validation handler answers {"code": "invalid_request"}. A route that doesn't declare its
    # 422 publishes FastAPI's {"detail": [...]} shape to the generated client instead.
    spec = create_app().openapi()

    wrong = [
        f"{method.upper()} {path} {status}"
        for path, operations in spec["paths"].items()
        for method, operation in operations.items()
        for status, answer in operation["responses"].items()
        if status.startswith(("4", "5"))
        and answer.get("content", {}).get("application/json", {}).get("schema")
        != {"$ref": "#/components/schemas/Error"}
    ]
    assert wrong == []


def test_operation_ids_are_tag_and_route_name() -> None:
    spec = create_app().openapi()

    assert spec["paths"]["/api/healthz"]["get"]["operationId"] == "health-healthz"


def test_committed_openapi_json_matches_the_app() -> None:
    # The web client is generated from this file. If this fails, run `make openapi` and commit.
    stale = "backend/openapi.json is stale: run `make openapi`"
    assert CONTRACT.read_text() == openapi_document(), stale


def test_openapi_document_is_stable_json() -> None:
    document = openapi_document()

    assert document.endswith("\n")
    assert json.loads(document)["info"]["title"] == "ziftbook"


def test_healthz_response_is_a_named_schema_with_required_fields() -> None:
    spec = create_app().openapi()
    response = spec["paths"]["/api/healthz"]["get"]["responses"]["200"]
    ref = response["content"]["application/json"]["schema"]["$ref"]

    schema = spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]
    assert schema["required"] == ["status", "version"]


# 31: FENCE. Wrong impl: add internal_note to a public schema (or publish ClientOut from a
# /api/public route) and run `make openapi`.
def test_no_public_schema_exposes_an_internal_note() -> None:
    spec = json.loads(CONTRACT.read_text())
    public_operations = [
        operation
        for path, operations in spec["paths"].items()
        if path.startswith("/api/public")
        for operation in operations.values()
    ]
    reachable = schemas_reachable_from(spec, *public_operations)
    offending = [
        name
        for name in reachable
        if "internal_note" in spec["components"]["schemas"][name].get("properties", {})
    ]
    assert offending == []


# 32: FENCE, load-bearing (test 21 is its unit echo). Wrong impl: add
# user_id: UUID | None = None to ClientIn and run `make openapi`.
def test_no_request_schema_accepts_a_user_id() -> None:
    spec = json.loads(CONTRACT.read_text())
    starts: list[Any] = []
    named_user_id = False
    for operations in spec["paths"].values():
        for operation in operations.values():
            for parameter in operation.get("parameters", []):
                if parameter.get("name") == "user_id":
                    named_user_id = True
                starts.append(parameter)
            if "requestBody" in operation:
                starts.append(operation["requestBody"])
    assert not named_user_id

    # Never responses: MemberOut and SessionOut legitimately carry user_id there.
    reachable = schemas_reachable_from(spec, *starts)
    offending = [
        name
        for name in reachable
        if "user_id" in spec["components"]["schemas"][name].get("properties", {})
    ]
    assert offending == []
