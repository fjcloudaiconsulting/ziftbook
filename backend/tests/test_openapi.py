import json
from pathlib import Path

from app.main import create_app, openapi_document

CONTRACT = Path(__file__).parent.parent / "openapi.json"


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
