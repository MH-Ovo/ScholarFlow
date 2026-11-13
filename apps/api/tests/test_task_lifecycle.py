"""任务队列：创建 → 取消 → 重试 → 删除；状态与步骤收尾。"""
from fastapi.testclient import TestClient


def _create(client: TestClient, headers, query="task lifecycle test") -> str:
    r = client.post(
        "/api/v1/research", json={"query": query, "lang": "en"}, headers=headers
    )
    assert r.status_code == 200
    return r.json()["task_id"]


def test_create_queued_and_cancel(client: TestClient, key_a):
    task_id = _create(client, key_a)
    detail = client.get(f"/api/v1/research/{task_id}", headers=key_a).json()
    assert detail["status"] == "queued"
    assert any(s["step_key"] == "report_write" for s in detail["steps"])

    r = client.post(f"/api/v1/research/{task_id}/cancel", headers=key_a)
    assert r.status_code == 200
    assert r.json()["result"] == "cancelled"
    assert client.get(f"/api/v1/research/{task_id}", headers=key_a).json()["status"] == "cancelled"


def test_retry_and_delete(client: TestClient, key_a):
    task_id = _create(client, key_a)
    client.post(f"/api/v1/research/{task_id}/cancel", headers=key_a)

    r = client.post(f"/api/v1/research/{task_id}/retry", headers=key_a)
    assert r.status_code == 200
    assert client.get(f"/api/v1/research/{task_id}", headers=key_a).json()["status"] == "queued"

    assert client.delete(f"/api/v1/research/{task_id}", headers=key_a).status_code == 200
    assert client.get(f"/api/v1/research/{task_id}", headers=key_a).status_code == 404


def test_followup_rejected_while_queued(client: TestClient, key_a):
    task_id = _create(client, key_a)
    r = client.post(
        f"/api/v1/research/{task_id}/followup",
        json={"query": "followup", "lang": "en"},
        headers=key_a,
    )
    assert r.status_code == 409
    client.post(f"/api/v1/research/{task_id}/cancel", headers=key_a)
