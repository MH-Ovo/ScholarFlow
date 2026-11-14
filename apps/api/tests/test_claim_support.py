"""逐句核查判定：unsupported 列表与 claim 文本的匹配规则。"""
from app.agents.nodes import _claim_supported


def test_claim_supported_matches_normalized_text():
    assert _claim_supported("RAG 有三大范式。", []) is True
    assert _claim_supported("RAG 有三大范式。", ["RAG 有三大范式"]) is False
    assert _claim_supported("RAG 有三大范式。", ["完全无关的论断"]) is True
    # 长句前缀匹配（核查模型常返回截断句）
    assert _claim_supported("RAG 的经典三大范式为 Naive RAG、Advanced RAG 和 Modular RAG。", ["RAG 的经典三大范式"]) is False
