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


# 31/40: FENCE, narrowed in ZIF-51. Wrong impl: add internal_note to a public RESPONSE schema (or
# publish ClientOut from a /api/public route) and run `make openapi`.
#
# phone, email and locale are fenced alongside it, so ZIF-51's "never echo these publicly" is
# executable rather than prose: find_or_create returns the *stored* phone and locale, so a public
# answer carrying either hands an unauthenticated stranger a third party's contact details. A
# public route that genuinely needs one of these names it in its own schema and edits this list,
# deliberately, in that ticket.
PRIVATE_TO_THE_CONSOLE = ("internal_note", "phone", "email", "locale")


def test_no_public_response_exposes_a_clients_private_field() -> None:
    # Renamed from ..._no_public_schema_... and narrowed to RESPONSES in ZIF-51. The rule was always
    # "never ECHO these publicly" (find_or_create returns the *stored* phone and locale, so a public
    # answer carrying either hands a stranger a third party's contact details). A public REQUEST
    # legitimately takes an address: that is what guest booking is. The request side is fenced by
    # the next test, with an explicit allowlist, so neither direction is unguarded.
    spec = json.loads(CONTRACT.read_text())
    responses = [
        operation["responses"]
        for path, operations in spec["paths"].items()
        if path.startswith("/api/public")
        for operation in operations.values()
    ]
    reachable = schemas_reachable_from(spec, *responses)
    offending = [
        (name, field)
        for name in reachable
        for field in PRIVATE_TO_THE_CONSOLE
        if field in spec["components"]["schemas"][name].get("properties", {})
    ]
    assert offending == []


# 41: FENCE, new in ZIF-51. Wrong impl: (a) publish ClientIn from a public route and run
# `make openapi`; (b) remove all three of email, phone and locale from BookingIn.
BOOKING_REQUEST = {"BookingIn"}


def test_the_only_public_request_schema_with_contact_details_is_the_booking() -> None:
    spec = json.loads(CONTRACT.read_text())
    bodies = [
        operation["requestBody"]
        for path, operations in spec["paths"].items()
        if path.startswith("/api/public")
        for operation in operations.values()
        if "requestBody" in operation
    ]
    reachable = schemas_reachable_from(spec, *bodies)
    carrying = {
        name
        for name in reachable
        for field in PRIVATE_TO_THE_CONSOLE
        if field in spec["components"]["schemas"][name].get("properties", {})
    }
    assert carrying == BOOKING_REQUEST


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
