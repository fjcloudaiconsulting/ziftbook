import json
from pathlib import Path

from app.main import create_app, openapi_document

CONTRACT = Path(__file__).parent.parent / "openapi.json"


def test_operation_ids_are_tag_and_route_name() -> None:
    spec = create_app().openapi()

    assert spec["paths"]["/api/healthz"]["get"]["operationId"] == "health-healthz"


def test_committed_openapi_json_matches_the_app() -> None:
    # The web client is generated from this file. If this fails, run `make openapi` and commit.
    stale = "apps/api/openapi.json is stale: run `make openapi`"
    assert CONTRACT.read_text() == openapi_document(), stale


def test_openapi_document_is_stable_json() -> None:
    document = openapi_document()

    assert document.endswith("\n")
    assert json.loads(document)["info"]["title"] == "ziftbook"
