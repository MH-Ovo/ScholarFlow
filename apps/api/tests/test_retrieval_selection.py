"""来源窗口选择：同章节去重、名额补位、外部来源不误合并。"""

from app.agents.nodes import _parent_key, _pick_parent_diverse


def _kb(doc: int, chunk: int, parent: str, score: float) -> dict:
    return {
        "document_id": doc,
        "chunk_index": chunk,
        "parent_text": parent,
        "type": "enterprise",
        "relevance": score,
    }


def test_prefers_distinct_parents_and_keeps_order():
    cands = [
        _kb(1, 1, "A" * 50, 0.9),
        _kb(1, 2, "A" * 50, 0.85),
        _kb(2, 5, "B" * 50, 0.7),
        _kb(3, 0, "C" * 50, 0.6),
    ]
    picked = _pick_parent_diverse(cands, 3)
    assert [(c["document_id"], c["chunk_index"]) for c in picked] == [(1, 1), (2, 5), (3, 0)]


def test_fills_remaining_slots_when_parents_run_out():
    cands = [
        _kb(1, 1, "A" * 50, 0.9),
        _kb(1, 2, "A" * 50, 0.8),
        _kb(2, 1, "B" * 50, 0.7),
    ]
    picked = _pick_parent_diverse(cands, 4)
    assert len(picked) == 3
    assert len({c["document_id"] for c in picked}) == 2


def test_external_sources_are_not_collapsed():
    cands = [
        {"type": "web", "url": "https://a", "title": "a", "parent_text": ""},
        {"type": "web", "url": "https://b", "title": "b", "parent_text": ""},
        {"type": "paper", "url": "arxiv://1", "title": "p", "parent_text": ""},
    ]
    assert len({_parent_key(c) for c in cands}) == 3
    assert len(_pick_parent_diverse(cands, 3)) == 3


def test_limit_zero_returns_empty():
    assert _pick_parent_diverse([_kb(1, 1, "A", 0.9)], 0) == []
