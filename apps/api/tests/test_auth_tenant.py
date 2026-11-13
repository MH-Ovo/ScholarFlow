"""认证与租户隔离。"""
from fastapi.testclient import TestClient


def test_requires_auth(client: TestClient):
    assert client.get("/api/v1/research").status_code == 401
    assert client.get("/api/v1/auth/me").status_code == 401


def test_login_and_me(client: TestClient):
    r = client.post("/api/v1/auth/login", json={"api_key": "key-a"})
    assert r.status_code == 200
    token = r.json()["token"]
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["tenant_id"] == "tenant-a"
    assert me.json()["user_id"] == "user-a"

    bad = client.post("/api/v1/auth/login", json={"api_key": "nope"})
    assert bad.status_code == 401


def test_tenant_isolation(client: TestClient, key_a, key_b):
    r = client.post(
        "/api/v1/research", json={"query": "tenant a only", "lang": "en"}, headers=key_a
    )
    assert r.status_code == 200
    task_id = r.json()["task_id"]

    assert [t["id"] for t in client.get("/api/v1/research", headers=key_a).json()] == [task_id]
    assert client.get("/api/v1/research", headers=key_b).json() == []
    assert client.get(f"/api/v1/research/{task_id}", headers=key_b).status_code == 404
    assert client.get(f"/api/v1/research/{task_id}", headers=key_a).status_code == 200

    assert client.delete(f"/api/v1/research/{task_id}", headers=key_b).status_code == 404
    assert client.delete(f"/api/v1/research/{task_id}", headers=key_a).status_code == 200
