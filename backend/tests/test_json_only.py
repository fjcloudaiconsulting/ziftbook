import pytest
from fastapi.testclient import TestClient

from app.main import create_app

UNSAFE = ["POST", "PUT", "PATCH", "DELETE"]


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.add_api_route("/api/probe", lambda: None, methods=["GET", "HEAD", *UNSAFE], tags=["probe"])
    return TestClient(app)


@pytest.mark.parametrize("method", UNSAFE)
@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "text/plain",
        "application/x-www-form-urlencoded",
        "multipart/form-data; boundary=x",
        # Still a form type to the browser (no preflight); a substring test would pass it.
        "text/plain; application/json",
        "text/plain; charset=application/json",
    ],
)
def test_state_changing_requests_that_are_not_json_are_rejected(
    client: TestClient, method: str, content_type: str | None
) -> None:
    headers = {"content-type": content_type} if content_type else {}

    response = client.request(method, "/api/probe", headers=headers)

    assert response.status_code == 415
    assert response.json() == {"code": "unsupported_media_type"}


@pytest.mark.parametrize("method", UNSAFE)
@pytest.mark.parametrize(
    "content_type", ["Application/JSON; charset=utf-8", "application/json ; x=y"]
)
def test_json_with_parameters_is_accepted(
    client: TestClient, method: str, content_type: str
) -> None:
    response = client.request(method, "/api/probe", headers={"content-type": content_type})

    assert response.status_code == 200


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_safe_methods_need_no_content_type(client: TestClient, method: str) -> None:
    # OPTIONS gets 405 from FastAPI; it only must not be the CSRF check.
    assert client.request(method, "/api/probe").status_code != 415


def test_cross_origin_requests_are_never_allowed(client: TestClient) -> None:
    response = client.options(
        "/api/probe",
        headers={"origin": "https://evil.example", "access-control-request-method": "POST"},
    )

    assert "access-control-allow-origin" not in response.headers
