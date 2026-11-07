from fastapi.testclient import TestClient

from app.main import app


def test_health_ok():
    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["kb"]["state"] in {"empty", "ready", "not_indexed"}
    assert resp.headers.get("x-request-id")
