from fastapi.testclient import TestClient

from app.main import app


def test_home_page_is_served() -> None:
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "Migrador de switches" in response.text


def test_capabilities_expose_blocked_groups() -> None:
    response = TestClient(app).get("/api/capabilities")
    assert response.status_code == 200
    assert any(item["id"] == "l3" and not item["readable"] for item in response.json())
