"""P0 acceptance: GET /health (spec section 58)."""

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod


@pytest.fixture
def client():
    from app.main import create_app

    return TestClient(create_app())


def test_health_structure_and_ok(client, monkeypatch):
    monkeypatch.setattr(main_mod, "check_database", lambda: True)
    monkeypatch.setattr(main_mod, "check_redis", lambda url: True)

    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ok", "database": True, "redis": True}


def test_health_reports_dependencies_down(client, monkeypatch):
    monkeypatch.setattr(main_mod, "check_database", lambda: False)
    monkeypatch.setattr(main_mod, "check_redis", lambda url: False)

    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] is False
    assert body["redis"] is False


def test_check_redis_returns_bool():
    from app.main import check_redis

    # Unreachable URL must fail gracefully (bool, no exception).
    assert check_redis("redis://127.0.0.1:1/0") is False
