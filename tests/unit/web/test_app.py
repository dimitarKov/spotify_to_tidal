# tests/unit/web/test_app.py

import pytest
from fastapi.testclient import TestClient

from spotify_to_tidal.web.app import SESSION_COOKIE, create_app
from spotify_to_tidal.web.settings import WebSettings

INVITE_CODE = "let-me-in-please"
SECRET = "x" * 32


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(WebSettings(invite_code=INVITE_CODE, session_secret=SECRET)), follow_redirects=False)


def enter_invite_code(client: TestClient, code: str = INVITE_CODE):
    return client.post("/welcome", data={"invite_code": code})


def test_healthz(client: TestClient):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


def test_home_requires_invite_code(client: TestClient):
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/welcome"


def test_welcome_page(client: TestClient):
    response = client.get("/welcome")
    assert response.status_code == 200
    assert 'name="invite_code"' in response.text
    assert "/static/htmx.min.js" in response.text


def test_wrong_invite_code(client: TestClient):
    response = enter_invite_code(client, "wrong-code")
    assert response.status_code == 403
    assert "isn&#39;t right" in response.text
    assert client.get("/").status_code == 303  # still not let in


def test_missing_invite_code(client: TestClient):
    response = client.post("/welcome", data={})
    assert response.status_code == 403


def test_right_invite_code_lets_you_in(client: TestClient):
    response = enter_invite_code(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    home = client.get("/")
    assert home.status_code == 200
    assert "connect your Spotify and Tidal accounts" in home.text
    # and the welcome page now sends you home
    assert client.get("/welcome").headers["location"] == "/"


def test_invite_code_ignores_surrounding_whitespace(client: TestClient):
    assert enter_invite_code(client, f"  {INVITE_CODE}\n").status_code == 303


def test_logout(client: TestClient):
    enter_invite_code(client)
    response = client.post("/logout")
    assert response.status_code == 303
    assert response.headers["location"] == "/welcome"
    assert client.get("/").status_code == 303


def test_session_cookie_flags(client: TestClient):
    cookie = enter_invite_code(client).headers["set-cookie"].lower()
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "secure" not in cookie


def test_session_cookie_secure_when_https_only():
    client = TestClient(create_app(WebSettings(invite_code=INVITE_CODE, session_secret=SECRET, https_only=True)),
                        base_url="https://testserver", follow_redirects=False)
    assert "secure" in enter_invite_code(client).headers["set-cookie"].lower()


def test_tampered_session_cookie_is_rejected(client: TestClient):
    client.cookies.set(SESSION_COOKIE, "eyJpbnZpdGVkIjogdHJ1ZX0=.forged.signature")
    assert client.get("/").status_code == 303


def test_htmx_is_served(client: TestClient):
    response = client.get("/static/htmx.min.js")
    assert response.status_code == 200
    assert 'version:"2.0.11"' in response.text


def test_no_api_docs_exposed(client: TestClient):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
