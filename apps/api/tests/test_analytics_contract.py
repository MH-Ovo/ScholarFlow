"""P1 看板与检索参数契约。"""
from app.core.config import get_settings


def test_analytics_summary_shape(client, key_a):
    resp = client.get("/api/v1/analytics/summary", headers=key_a)
    assert resp.status_code == 200
    body = resp.json()
    assert {"total_research", "knowledge_sources", "documents_processed", "accuracy_rate"} <= set(body)
    assert set(body["latency"]) == {"avg_sec", "p95_sec", "samples"}
    assert set(body["llm"]) == {"tokens_in", "tokens_out", "calls", "cost_usd"}


def test_candidate_pool_is_expanded():
    settings = get_settings()
    assert settings.kb_candidate_k >= 40
    assert settings.kb_candidate_k_escalated >= settings.kb_candidate_k
