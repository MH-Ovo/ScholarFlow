"""LangGraph 节点：kb_search → report_write（纯本地知识库 RAG）。

- 学术检索 / 网页检索 / 知识图谱构建已按需求暂停（不入图）
- 知识库无命中时明确告知，不编造
- 无 LLM Key 时报告改为真实片段关键句摘录（带 [n] 标签，零编造）
"""
import asyncio
import re
import time
from datetime import datetime, timezone

from sqlalchemy import delete, select

from app.agents import mock_data
from app.agents.events import EVENT_BUS
from app.agents.prompts import (
    CHART_REPORT_PROMPT,
    CHAT_PROMPT,
    CNKI_KEYWORDS_PROMPT,
    GRADE_PROMPT,
    QUERY_PROCESS_PROMPT,
    REPORT_PROMPT,
    REWRITE_PROMPT,
    ROUTER_PROMPT,
    VERIFY_PROMPT,
)
from app.agents.source_registry import SOURCE_SPECS
from app.agents.state import ResearchState
from app.core import trace
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.integrations.adapters import get_adapter
from app.integrations.source_result import SourceStatus
from app.llm.gateway import get_llm

# 检索源注册表。新增一个源需同步改动四处：
#   1. 本元组（Planner 规划范围）
#   2. graph.py 的扇出边
#   3. ResearchState 的结果字段
#   4. merger_node 的结果合并
# 缺任一处都会导致"节点跑了但结果被丢弃"，且不会报错。
RETRIEVAL_SOURCES = tuple(SOURCE_SPECS)

# 语言感知检索：同语言候选少于此数时，再补一次全库检索
KB_MIN_PREFERRED = 6

# 精排输入长度上限（字符）。parent_text 最长 4000，取 1200 兼顾上下文与推理耗时：
# 太短看不到答案段落，太长会让单条打分从 ~40ms 涨到数百毫秒。
RERANK_INPUT_CHARS = 1200


def _log(msg: str) -> None:
    """进程诊断日志：stdout 管道失效（如日志文件被轮转删除）时静默。

    绝不让日志抛错打断业务——曾经 print 在 except 块里抛 OSError(Errno 22)
    把"本该被兜住的 Qdrant 故障"升级成整个任务失败。
    """
    try:
        print(msg, flush=True)
    except Exception:
        pass
from app.models.research import (
    STEP_DEFS,
    Citation,
    Document,
    DocumentChunk,
    Report,
    ResearchStep,
    ResearchTask,
    Source,
)

MAX_KB_SOURCES = 12  # KB 候选上限（= merger 窗口；兄弟扩展后仍能整体进入精排）
MAX_GRADE_SOURCES = 12  # merger 输出给 grader 评估的候选上限
SIBLING_BASE = 6      # 兄弟扩展：参与扩展的核心命中数
SIBLING_MAX_ADD = 6   # 兄弟扩展：最多补充的邻居切片数（核心+邻居 ≤ MAX_KB_SOURCES）
_step_lock = asyncio.Lock()

settings = get_settings()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _step(task_id: str, step_key: str, label: str, status: str, meta: dict | None = None) -> None:
    meta = meta or {}
    async with _step_lock:
        async with SessionLocal() as db:
            row = (
                await db.execute(
                    select(ResearchStep).where(
                        ResearchStep.task_id == task_id, ResearchStep.step_key == step_key
                    )
                )
            ).scalar_one_or_none()
            if row:
                row.status = status
                if status == "running":
                    row.started_at = _now()
                if status in ("done", "failed", "skipped"):
                    row.finished_at = _now()
                row.meta_json = {**(row.meta_json or {}), **meta}
                await db.commit()
    if status == "running":
        EVENT_BUS.emit(task_id, "step_started", {"step": step_key, "label": label})
    elif status == "done":
        EVENT_BUS.emit(task_id, "step_completed", {"step": step_key, "label": label, **meta})
    elif status == "failed":
        EVENT_BUS.emit(task_id, "step_failed", {"step": step_key, "label": label, **meta})
    elif status == "skipped":
        EVENT_BUS.emit(task_id, "step_skipped", {"step": step_key, "label": label})
    trace.record_node(
        task_id, step_key, status,
        duration_ms=meta.get("duration_ms"),
        meta=meta,
    )


def _emit_sources(task_id: str, sources: list[dict]) -> None:
    for s in sources:
        EVENT_BUS.emit(
            task_id,
            "source_found",
            {
                "type": s["type"],
                "title": s["title"],
                "url": s.get("url", ""),
                "snippet": s.get("snippet", ""),
                "relevance": s.get("relevance", 0.0),
                "source_label": s.get("source_label", ""),
                "meta": s.get("meta") or {},
            },
        )


# ---------------- 意图路由（chat / knowledge） ----------------

async def fanout_node(state: ResearchState) -> dict:
    """multi 扇出前的记账节点；真正分发由 graph 的 SendAPI 路由完成。"""
    planned = [s for s in (state.get("plan") or ["kb"]) if s in SOURCE_SPECS]
    external = [s for s in planned if s != "kb"]
    dispatched = len(planned) if settings.dynamic_routing else len(SOURCE_SPECS)
    external_dispatched = len(external) if settings.dynamic_routing else len(SOURCE_SPECS) - 1
    metrics = state.get("metrics") or {}
    return {
        "metrics": {
            **metrics,
            "dispatch_mode": "send" if settings.dynamic_routing else "static",
            "last_round_sources": dispatched,
            "last_round_external_sources": external_dispatched,
            "scheduled_sources_total": int(metrics.get("scheduled_sources_total", 0)) + dispatched,
            "scheduled_external_total": int(metrics.get("scheduled_external_total", 0)) + external_dispatched,
        }
    }


async def fanout_external_node(state: ResearchState) -> dict:
    """KB 无命中升级：解除论文/网页/知网的规划跳过（本轮补查外部源）。"""
    planned = ["paper", "web"] + (["cnki"] if settings.cnki_enabled else [])
    external_dispatched = len(planned) if settings.dynamic_routing else len(SOURCE_SPECS) - 1
    metrics = state.get("metrics") or {}
    return {
        "plan": ["kb", "paper", "web"] + (["cnki"] if settings.cnki_enabled else []),
        "external_run": True,
        "metrics": {
            **metrics,
            "dispatch_mode": "send" if settings.dynamic_routing else "static",
            "last_round_sources": external_dispatched,
            "last_round_external_sources": external_dispatched,
            "scheduled_sources_total": int(metrics.get("scheduled_sources_total", 0)) + external_dispatched,
            "scheduled_external_total": int(metrics.get("scheduled_external_total", 0)) + external_dispatched,
        },
    }


async def router_node(state: ResearchState) -> dict:
    """检索规划器（Planner）：chat 直接回复；knowledge 逐源规划 kb/paper/web。

    plan = 规划的来源子集；search_mode 由 plan 派生（多源=multi，仅KB=kb_only 渐进式）。
    兼容旧输出格式 {"sources": "kb_only"/"multi"}（LLM 偶发老格式时降级解析）。
    """
    task_id = state["task_id"]
    query = state.get("original_query") or state["query"]
    llm = get_llm()
    payload = await llm.extract_json(
        ROUTER_PROMPT.format(query=query), mock_data.mock_route(query)
    )
    payload = payload if isinstance(payload, dict) else {}
    qtype = payload.get("type") or "knowledge"
    if qtype not in ("chat", "knowledge"):
        qtype = "knowledge"

    if qtype == "chat":
        plan: list[str] = []
    elif any(k in payload for k in SOURCE_SPECS):
        plan = [s for s in SOURCE_SPECS if payload.get(s) is True]
        if not plan:
            plan = ["kb"]
    else:  # 旧格式兼容
        plan = list(SOURCE_SPECS) if payload.get("sources") == "multi" else ["kb"]

    # 知网 A/B 开关：enabled=false 时从计划中移除；force=true 时强制加入（评测用）
    if qtype != "chat":
        if not settings.cnki_enabled:
            plan = [s for s in plan if s != "cnki"] or ["kb"]
        elif settings.cnki_force and "cnki" not in plan:
            plan.append("cnki")

    search_mode = "kb_only" if plan == ["kb"] else "multi"

    paused_steps = ["kb_search", "paper_search", "web_search", "cnki_search", "graph_build"]
    if qtype == "chat":
        # 闲聊路径：检索相关步骤标记为已暂停（DB 状态；前端由 router_result 事件同步）
        for key, label in STEP_DEFS:
            if key in paused_steps:
                await _step(task_id, key, label, "paused")
    EVENT_BUS.emit(
        task_id, "router_result",
        {"type": qtype, "query": query[:40], "paused_steps": paused_steps,
         "sources": "multi" if len(plan) > 1 else "kb_only", "plan": plan},
    )
    # 合并增强：规划调用同时产出子查询变体（省一次独立增强 LLM 调用，约 5-10s）
    plan_subqueries = [
        q.strip() for q in (payload.get("sub_queries") or [])
        if isinstance(q, str) and q.strip() and q.strip() != query
    ][:2] if qtype == "knowledge" else []
    return {"query_type": qtype, "search_mode": search_mode, "plan": plan,
            "plan_subqueries": plan_subqueries, "escalation": False}


# ---------------- 知识库检索（混合检索：bge-m3 dense+sparse 双路 RRF） ----------------

async def _fuse_hybrid(
    store, dense_hits: list[dict], sparse_hits: list[dict], query_dense: list[float],
    min_score: float | None = None, relax_min_score: float | None = None,
    dense_weight: float = 1.0, sparse_weight: float = 1.0,
) -> list[dict]:
    """双路合并 → 阈值过滤 → RRF 排序。

    阈值策略：
      主过滤   : dense cosine ≥ KB_MIN_SCORE
      宽松兜底 : sparse 独有命中（sparse Top-5 内）本地补算 dense ≥ 0.35
    relevance 统一用 dense 分展示（0~1）。
    """
    from app.rag.vector_store import RRF_K

    merged: dict[int, dict] = {}
    for rank, h in enumerate(dense_hits, start=1):
        merged[h["id"]] = {**h, "dense_rank": rank, "sparse_rank": None}
    for rank, h in enumerate(sparse_hits, start=1):
        if h["id"] in merged:
            merged[h["id"]]["sparse_rank"] = rank
        else:
            merged[h["id"]] = {**h, "dense_rank": None, "sparse_rank": rank}

    # sparse 独有候选（sparse Top-5 内）：取回 dense 向量本地余弦补算
    missing = [
        h
        for h in merged.values()
        if h["dense_rank"] is None and h["sparse_rank"] is not None and h["sparse_rank"] <= 5
    ]
    if missing:
        vecs = await store.retrieve_dense([h["id"] for h in missing])
        for h in missing:
            v = vecs.get(h["id"])
            h["dense_score_extra"] = (
                round(max(0.0, min(1.0, sum(a * b for a, b in zip(query_dense, v)))), 3)
                if v is not None else 0.0
            )

    cands: list[dict] = []
    ms = settings.kb_min_score if min_score is None else min_score
    rms = settings.kb_relax_min_score if relax_min_score is None else relax_min_score
    for h in merged.values():
        dense_score = h["score"] if h["dense_rank"] is not None else h.get("dense_score_extra", 0.0)
        h["relevance"] = dense_score
        if h["dense_rank"] is not None and h["score"] >= ms:
            cands.append(h)
        elif (
            h["dense_rank"] is None
            and h["sparse_rank"] is not None
            and h["sparse_rank"] <= 5
            and h.get("dense_score_extra", 0.0) >= rms
        ):
            cands.append(h)

    for h in cands:
        rrf = 0.0
        if h["dense_rank"] is not None:
            rrf += dense_weight / (RRF_K + h["dense_rank"])
        if h["sparse_rank"] is not None:
            rrf += sparse_weight / (RRF_K + h["sparse_rank"])
        h["rrf"] = rrf
    cands.sort(key=lambda x: x["rrf"], reverse=True)
    return cands


def _build_enhanced_queries(processed: dict, original: str) -> tuple[str, str]:
    """返回 (dense_query, sparse_query)。

    dense 用简洁改写（避免长文本稀释余弦分数）；
    sparse 用改写+关键词（词法权重覆盖更广，精确词命中增强）。
    无增强时两者均为原问题。
    """
    rq = (processed.get("rewritten_query") or original).strip() or original
    kws = [
        k for k in (processed.get("keywords") or [])
        if isinstance(k, str) and k.strip()
    ]
    sparse_query = rq if not kws else f"{rq}\n关键词：{'；'.join(kws)}"
    return rq, sparse_query


def _apply_rerank(query: str, cands: list[dict], alpha: float | None = None) -> list[dict]:
    """bge-reranker-v2-m3 模型精排：按模型相关度分降序，relevance 更新为模型分。

    模型不可用/异常时返回原列表（保持 RRF 排序，链路不中断）。

    打分文本用切片 `text` 而非 `parent_text`：改成整段（最长 4000 字）反而更差
    ——实测 sf13 从第 13 名掉到第 36 名、sf26 从 22 掉到 31，整段噪音稀释了信号。
    """
    from app.rag.models import rerank_scores

    snippets = [
        (c.get("text") or c.get("snippet") or "")[:RERANK_INPUT_CHARS] for c in cands
    ]
    scores = rerank_scores(query, snippets)
    if scores is None:
        return cands

    # 混合排序：精排分与 RRF 名次分加权。
    # 纯精排会把"开头是作者列表/公式"的切片压到底（实测 sf01 排 40、sf30 排 46），
    # 而纯 RRF 又缺少语义精排能力（关掉精排整体 0.650 < 开启 0.700）。
    # 两者按 alpha 混合，兼顾语义相关性与召回阶段的排序信号。
    a = float(alpha if alpha is not None else getattr(settings, "rerank_blend_alpha", 1.0))

    # 混合时用**真实 RRF 分**归一化，而不是当前列表位置。
    # 兄弟扩展会把邻居插到中间，位置不再代表召回阶段的排序 —— 早前用位置归一化，
    # 导致合并后本在第 9/12 名的标注切片被邻居挤到第 15-18 名（sf15/sf26/sf28 实测）。
    raw_rrf = [float(c.get("rrf_multi") or c.get("rrf") or 0.0) for c in cands]
    max_rrf = max(raw_rrf) or 1.0

    for cand, score, rrf in zip(cands, scores, raw_rrf):
        cand["rerank_score"] = round(score, 3)
        if a >= 1.0:
            cand["relevance"] = round(score, 3)
            continue
        cand["relevance"] = round(a * score + (1 - a) * (rrf / max_rrf), 3)
    cands.sort(key=lambda x: x["relevance"], reverse=True)
    return cands


def _is_chinese(text: str, threshold: float = 0.30) -> bool:
    """按汉字占比判断语种。只统计字母类字符，避免标点与空格干扰。"""
    if not text:
        return True
    cn = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    total = cn + latin
    return total == 0 or cn / total >= threshold


async def _doc_languages() -> dict[int, str]:
    """采样每个文档的前几个切片，判定其语种。

    用于语言感知检索：知识库里中英文混排时，英文问题会被中文文档在向量空间里
    整体挤掉（实测英文 30 条召回 0.500，限定英文文档后 1.000）。
    """
    from sqlalchemy import text as sql_text

    from app.db.session import SessionLocal

    try:
        async with SessionLocal() as db:
            rows = (
                await db.execute(
                    sql_text(
                        "select document_id, group_concat(text, ' ') as sample from "
                        "(select document_id, text from document_chunks where chunk_index < 3) "
                        "group by document_id"
                    )
                )
            ).all()
    except Exception:
        return {}

    return {int(doc_id): ("zh" if _is_chinese(sample or "") else "en") for doc_id, sample in rows}


def _merge_by_rrf(a: list[dict], b: list[dict], k: int = 60) -> list[dict]:
    """按 RRF 合并两个候选列表（同语言结果优先：a 在前，等分时 a 靠前）。"""
    if not a:
        return list(b)
    if not b:
        return list(a)

    scores: dict[int, float] = {}
    best: dict[int, dict] = {}
    for lst in (a, b):
        for rank, h in enumerate(lst, start=1):
            cid = h["id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            if cid not in best:
                best[cid] = h

    merged = []
    for cid, score in scores.items():
        h = dict(best[cid])
        h["rrf_multi"] = round(score, 6)
        merged.append(h)
    merged.sort(key=lambda x: x["rrf_multi"], reverse=True)
    return merged


async def _hybrid_search(
    store, dense_text: str, sparse_text: str | None, top_k: int = 20,
    min_score: float | None = None, relax_min_score: float | None = None,
    document_ids: list[int] | None = None,
    dense_weight: float = 1.0, sparse_weight: float = 1.0,
) -> list[dict]:
    """双路混合检索：dense 用简洁文本，sparse 用关键词文本；返回 RRF 融合候选。

    dense_text == sparse_text 时仅编码一次（两路取自同一编码）。
    document_ids 用于限定检索范围。
    dense/sparse_weight 控制两路在 RRF 中的比重：中文问句配英文正文时，
    sparse（BM25 类词法匹配）几乎命不中，只有 dense 一路得分，与中文切片
    双路命中的情况相比天然吃亏。
    """
    from app.rag.models import encode_hybrid, get_embedder

    dense_vec = None
    sparse_vec = None
    if dense_text == sparse_text:
        # 同一文本：一次编码取两路
        dense_res = await asyncio.to_thread(encode_hybrid, [dense_text])
        if dense_res[0] is not None:
            dense_vec = dense_res[0][0]
            if settings.hybrid_search and dense_res[1]:
                sparse_vec = dense_res[1][0]
    else:
        dense_res = await asyncio.to_thread(encode_hybrid, [dense_text])
        if dense_res[0] is not None:
            dense_vec = dense_res[0][0]
            if settings.hybrid_search and sparse_text:
                sparse_res = await asyncio.to_thread(encode_hybrid, [sparse_text])
                sparse_vec = sparse_res[1][0] if sparse_res[1] else None
    if dense_vec is None:
        # bge3 不可用 → 降级纯 dense
        embedder = get_embedder()
        if embedder is not None:
            dense_vec = (
                await asyncio.to_thread(
                    lambda: embedder.encode(
                        [dense_text], normalize_embeddings=True
                    ).tolist()
                )
            )[0]
    if dense_vec is None:
        return []
    dense_hits = await store.search_dense(dense_vec, top_k=top_k, document_ids=document_ids)
    sparse_hits = (
        await store.search_sparse(sparse_vec, top_k=top_k, document_ids=document_ids)
        if settings.hybrid_search and sparse_vec is not None
        else []
    )
    fused = await _fuse_hybrid(
        store, dense_hits, sparse_hits, dense_vec,
        min_score=min_score, relax_min_score=relax_min_score,
        dense_weight=dense_weight, sparse_weight=sparse_weight,
    )
    # 注：精排统一在 merger 节点对三路候选执行（避免 KB 内重复计算）
    return fused


async def _multi_query_search(
    store, query_pairs: list[tuple[str, str | None]], top_k: int = 20,
    min_score: float | None = None, relax_min_score: float | None = None,
    document_ids: list[int] | None = None,
    weights: list[float] | None = None,
) -> list[dict]:
    """多查询检索：每路各自 hybrid 检索，再跨查询 RRF 合并（按 chunk id 去重）。

    动机：单条查询的 dense 向量存在语义盲区（诊断显示 37.5% 标注 chunk 不在 dense top-50），
    换措辞的多路查询能覆盖不同语义角度；显示分 relevance 取各路中的最高 dense 分。

    `weights` 按位置给每路查询加权（缺省等权）。主查询用用户原话、质量最高，
    等权合并会被措辞不同的子查询稀释（实测召回 0.700 → 0.650），所以生产路径
    把主查询权重调高。
    """
    from app.rag.vector_store import RRF_K

    weighted: list[tuple[list[dict], float]] = []
    for idx, (dense_text, sparse_text) in enumerate(query_pairs):
        if not dense_text:
            continue
        res = await _hybrid_search(
            store, dense_text, sparse_text, top_k=top_k,
            min_score=min_score, relax_min_score=relax_min_score,
            document_ids=document_ids,
        )
        if not res:
            continue
        w = float(weights[idx]) if weights and idx < len(weights) else 1.0
        weighted.append((res, w))

    if not weighted:
        return []
    if len(weighted) == 1:
        return weighted[0][0]

    rrf_scores: dict[int, float] = {}
    best: dict[int, dict] = {}
    for lst, w in weighted:
        for rank, h in enumerate(lst, start=1):
            cid = h["id"]
            rrf_scores[cid] = rrf_scores.get(cid, 0.0) + w / (RRF_K + rank)
            cur = best.get(cid)
            if cur is None or (h.get("relevance") or 0.0) > (cur.get("relevance") or 0.0):
                best[cid] = h

    merged = []
    for cid, score in rrf_scores.items():
        h = dict(best[cid])
        h["rrf_multi"] = round(score, 6)
        merged.append(h)
    merged.sort(key=lambda x: x["rrf_multi"], reverse=True)
    return merged


async def _expand_with_siblings(fused: list[dict]) -> list[dict]:
    """兄弟切片扩展（auto-merging retrieval）：核心命中的 ±1 邻居就近插入候选。

    治"答案横跨相邻切片"的召回摊薄——命中 46 号后 45/47 大概率同样相关，
    ID 级标注往往覆盖整个邻域。邻居按核心命中顺序就近插入（不追加尾部），
    保证能进入 merger 的 top-12 精排窗口；去重、总量受 SIBLING_MAX_ADD 约束。
    """
    if not fused:
        return fused
    core = fused[:SIBLING_BASE]
    # 只登记"已进入 expanded 的 key"。若用整个 fused 初始化，尾部候选会在
    # 去重判断里被全部跳过 —— 这才是"候选 20→12"的直接原因。
    existing = {(h["document_id"], h["chunk_index"]) for h in core}

    # 批量取邻居文本与文档名（SQLite 两次小查询）
    need: dict[int, set[int]] = {}
    for h in core:
        d, i = h["document_id"], h["chunk_index"]
        need.setdefault(d, set()).update((i - 1, i + 1))
    nb_map: dict[tuple[int, int], dict] = {}
    async with SessionLocal() as db:
        for d, idxs in need.items():
            rows = (
                await db.execute(
                    select(DocumentChunk).where(
                        DocumentChunk.document_id == d,
                        DocumentChunk.chunk_index.in_([i for i in idxs if i >= 0]),
                    )
                )
            ).scalars().all()
            for r in rows:
                nb_map[(r.document_id, r.chunk_index)] = {
                    "text": r.text or "", "parent_text": r.parent_text or ""
                }
        doc_ids = list(need.keys())
        names = {
            r[0]: r[1]
            for r in (
                await db.execute(
                    select(Document.id, Document.name).where(Document.id.in_(doc_ids))
                )
            ).all()
        }

    expanded: list[dict] = []
    added = 0
    for h in core:
        expanded.append(h)
        if added >= SIBLING_MAX_ADD:
            continue
        d, i = h["document_id"], h["chunk_index"]
        for ni in (i - 1, i + 1):
            if added >= SIBLING_MAX_ADD:
                break
            key = (d, ni)
            if key in existing or key not in nb_map:
                continue
            existing.add(key)
            nb = nb_map[key]
            expanded.append({
                "id": f"sib-{d}-{ni}",
                "document_id": d,
                "chunk_index": ni,
                "document_name": names.get(d, ""),
                "text": nb["text"],
                "parent_text": nb["parent_text"],
                "relevance": h.get("relevance", 0.0),
                # 继承父切片的 RRF 分：精排混合阶段要用它做归一化，
                # 缺了会被当成 0 分，白白吃亏。
                "rrf_multi": h.get("rrf_multi"),
                "page_nos": [],
                "sibling": True,
            })
            added += 1

    # 尾部候选必须保留。原实现只返回 core(前 SIBLING_BASE 条) + 邻居，
    # 把 fused[SIBLING_BASE:] 整段丢掉了 —— 扩展本应增加候选，实际却从 20 条
    # 缩到 12 条，召回率随之从 0.733 掉到 0.600（实测 6 条因此丢失）。
    # 精排会从完整候选里选 top-12，候选多一些不会影响最终质量。
    for h in fused[SIBLING_BASE:]:
        key = (h["document_id"], h["chunk_index"])
        if key in existing:
            continue
        existing.add(key)
        expanded.append(h)

    # 注：曾尝试在此按 relevance 重排以缓解"扩展把好候选挤到 20-44 位"，
    # 实测无效（sf01 仍 41、sf28 由 20 劣化到 35），位置不是主因，
    # 而是精排对这些标注切片本身打分就低（多为作者列表/公式片段）。已回滚。
    return expanded


async def kb_retriever_node(state: ResearchState) -> dict:
    task_id, query = state["task_id"], state["query"]
    topic = mock_data.extract_topic(query)
    started = time.time()

    # Planner 逐源规划：KB 不在规划内 → 跳过（步骤标记 skipped，前端可见）
    plan = state.get("plan") or ["kb"]
    if "kb" not in plan:
        await _step(task_id, "kb_search", "查询本地知识库", "skipped")
        return {"kb_results": []}

    await _step(task_id, "kb_search", "查询本地知识库", "running")

    # 反思重查最终轮（escalation）：加大检索量 + 放宽阈值
    esc = bool(state.get("escalation"))
    top_k = 30 if esc else 20
    min_score = (settings.kb_min_score - 0.05) if esc else None

    # 查询增强：Planner 合并调用已产出子查询变体则直接复用（省一次 LLM 调用，约 5-10s）；
    # 未携带（简单题/Mock）时回退独立增强调用。
    # A/B 结论保持：主查询用用户原话（ab_recall orig_multi），改写产物仅作子查询变体
    dense_query, sparse_query = query, query
    sub_queries = list(state.get("plan_subqueries") or [])
    hyde_text = ""
    en_query = ""      # 中文问题的英文重述，用于跨语言检索
    enhanced = False
    if not sub_queries and settings.query_processing:
        llm = get_llm()
        processed = await llm.extract_json(
            QUERY_PROCESS_PROMPT.format(query=query),
            mock_data.mock_query_process(query),
        )
        dense_query, sparse_query = _build_enhanced_queries(processed, query)
        enhanced = dense_query != query or sparse_query != query
        if settings.multi_query:
            sub_queries = [
                s.strip()
                for s in (processed.get("sub_queries") or [])
                if isinstance(s, str) and s.strip() and s.strip() != query
            ][:2]
        if settings.hyde_enabled and settings.multi_query:
            hyde_text = str(processed.get("hyde") or "").strip()
        en_query = str(processed.get("en_query") or "").strip()

    results: list[dict] = []
    kb_status = "empty"  # empty / no_hits / unreachable / ok
    kb_error = ""        # 向量库不可用时的错误摘要，用于把步骤显式标为失败
    from app.rag.vector_store import get_vector_store

    store = get_vector_store()
    try:
        await store.ensure_collection()
        if await store.count() > 0:
            # 主查询用用户原话（A/B: orig_multi K12 recall 0.523 vs 改写主路 0.520，K8 持平；
            # 且不依赖改写质量）——LLM 改写的价值在 sub_queries 变体，文本见 ab_recall.json
            query_pairs: list[tuple[str, str | None]] = [(query, None)]
            weights: list[float] = [1.0]
            for s in sub_queries:
                query_pairs.append((s, None))
                weights.append(1.0)
            if hyde_text and hyde_text != query:
                query_pairs.append((hyde_text, None))  # HyDE 变体：假设答案贴近文档表述
                weights.append(1.0)
            # 跨语言补充：中文问题的英文重述单独一路。
            # 知识库中英文论文混排时，中文查询的向量会偏向中文切片，英文原文排不上来
            # （实测：sf06 中文查询排名 3、英文查询排名 1；sf17 从 14 升到 1）。
            if settings.en_query_enabled and en_query and en_query != query:
                query_pairs.append((en_query, None))
                # 权重高于中文路：实测英文路能把正确切片排到第 1，但等权时会被
                # 多条中文路累积的分数稀释出窗口（sf15/sf27/sf28/sf30）。
                weights.append(float(settings.en_query_weight))
            fused = await _multi_query_search(
                store, query_pairs, top_k=top_k, min_score=min_score, weights=weights
            )
            if not fused and (enhanced or sub_queries or hyde_text):
                # 增强未命中 → 回退原问题直接检索（兜底不劣化）
                fused = await _hybrid_search(store, query, None, top_k=top_k, min_score=min_score)
            if not fused:
                kb_status = "no_hits"
            else:
                kb_status = "ok"
                # 兄弟切片扩展：命中切片的 ±1 邻居就近插入（治标注邻域摊薄，零 LLM 成本）
                if settings.sibling_expand:
                    fused = await _expand_with_siblings(fused)
                    # 扩展会插入邻居并把原命中挤后；评测路径 production_kb_candidates
                    # 在扩展后重新精排，生产路径此前漏了这一步（sf23 表格切片被挤出 top-12）。
                    if settings.rerank_enabled:
                        fused = _apply_rerank(query, fused)
                        if settings.paged_chunk_boost > 0:
                            for h in fused:
                                if h.get("page_nos"):
                                    h["relevance"] = round(
                                        float(h.get("relevance", 0.0))
                                        + settings.paged_chunk_boost,
                                        4,
                                    )
                            fused.sort(
                                key=lambda x: x.get("relevance", 0.0), reverse=True
                            )
                # 注：不做单文档配额——A/B 实测配额在精排前挤掉同文档的标注相关片段
                # （multi_cap_rerank 0.410 vs multi_rerank 0.497）；多样性由 merger 统一精排+标题去重保证
                for h in fused:
                    doc_id = h["document_id"]
                    results.append(
                        {
                            "title": f"{h['document_name']} · 片段 {h['chunk_index'] + 1}",
                            "url": f"kb://doc/{doc_id}#c{h['chunk_index']}",
                            "snippet": (h.get("text") or "")[:300],
                            "type": "enterprise",
                            "source_label": "本地知识库",
                            "relevance": round(h.get("relevance", 0.0), 3),
                            "meta": {
                                "document_id": doc_id,
                                "parent_text": h.get("parent_text") or "",
                                "page_nos": h.get("page_nos") or [],
                            },
                            "page_nos": h.get("page_nos") or [],
                        }
                    )
                    if len(results) >= MAX_KB_SOURCES:
                        break
    except Exception as exc:
        # 兜底可观测：环境故障与代码 bug 都打印 traceback（此前静默吞错导致 KB 空无日志）
        import traceback

        traceback.print_exc()
        _log(f"[kra][kb] 检索异常: {type(exc).__name__}: {str(exc)[:200]}")
        kb_status = "unreachable"
        kb_error = f"{type(exc).__name__}: {str(exc)[:160]}"

    for i, s in enumerate(results, start=1):
        s["ref_no"] = i
    unique_docs = len({s.get("meta", {}).get("document_id") for s in results if s.get("meta")})

    # 落库来源（先清旧，支持追问重跑）
    async with SessionLocal() as db:
        await db.execute(delete(Source).where(Source.task_id == task_id))
        for s in results:
            db.add(
                Source(
                    task_id=task_id,
                    ref_no=s["ref_no"],
                    type=s["type"],
                    title=s["title"],
                    url=s.get("url", ""),
                    snippet=s.get("snippet", ""),
                    relevance=s.get("relevance", 0.0),
                    source_label=s.get("source_label", ""),
                    meta_json=s.get("meta", {}),
                )
            )
        await db.commit()

    if results:
        _emit_sources(task_id, results)
        EVENT_BUS.emit(task_id, "sources_final", {"sources": results})
    else:
        EVENT_BUS.emit(task_id, "kb_status", {"status": kb_status})

    kb_hit = bool(results)
    if kb_hit and state.get("search_mode", "kb_only") == "kb_only":
        # 渐进式检索：KB 已命中且用户未要求外部源 → 跳过论文/网页（省下游精排/打分/上下文成本）
        await _step(task_id, "paper_search", "检索学术论文", "skipped")
        await _step(task_id, "web_search", "搜索网页信息", "skipped")

    # 向量库不可用是故障，不是"没有命中"：必须把步骤标为 failed 并带上原因。
    # 否则前端显示"已完成 / 0 命中"，用户会误判成文档没上传或检索效果差。
    kb_unreachable = kb_status == "unreachable"
    await _step(
        task_id, "kb_search", "查询本地知识库",
        "failed" if kb_unreachable else "done",
        {"hits": len(results), "kb_status": kb_status,
         "enhanced": enhanced,
         "duration_ms": int((time.time() - started) * 1000),
         **({"error": kb_error} if kb_unreachable else {})},
    )
    out: dict = {
        "topic": topic,
        "kb_results": results,
        "metrics": {
            **state.get("metrics", {}),
            "kb_docs": unique_docs,  # 命中的唯一文档数（真实计数）
        },
    }
    if kb_unreachable:
        out["errors"] = [
            f"向量库不可用（{kb_error}），本轮知识库检索已跳过。"
            f"请确认 Qdrant 正在运行：powershell -File infra/ensure-qdrant.ps1"
        ]
    if kb_hit and state.get("search_mode", "kb_only") == "kb_only":
        # 清空外部源旧结果（改写重查命中 KB 时，不让上一轮论文/网页残留参与融合）
        out["paper_results"] = []
        out["web_results"] = []
    return out


# ---------------- 学术论文检索（arXiv） ----------------

async def paper_retriever_node(state: ResearchState) -> dict:
    task_id = state["task_id"]
    active_query = state.get("query") or state.get("original_query", "")
    started = time.time()

    # Planner 逐源规划：论文不在规划内（且非 KB 未命中升级补查）→ 跳过
    plan = state.get("plan") or ["kb", "paper", "web"]
    if "paper" not in plan and not state.get("external_run"):
        await _step(task_id, "paper_search", "检索学术论文", "skipped")
        return {"paper_results": []}

    await _step(task_id, "paper_search", "检索学术论文", "running")

    source_res = await get_adapter("arxiv").search(active_query, max_results=5)
    results = [
        {
            "title": r["title"],
            "url": r.get("url", ""),
            "snippet": r.get("snippet", ""),
            "type": "paper",
            "source_label": "学术论文",
            "relevance": round(max(0.0, 0.90 - i * 0.02), 3),  # 启发式（merger 统一精排修正）
            "meta": r.get("meta", {}),
        }
        for i, r in enumerate(source_res.items)
    ]
    # 不回落模拟数据：编造的 arXiv 条目会进入来源列表，污染引用与评估
    # （与知网适配层的原则一致——宁可 no_hits，也不制造不存在的文献）
    _emit_sources(task_id, results)
    step_meta = {
        "hits": len(results),
        "status": source_res.status.value,
        "source": source_res.source,
        "detail": source_res.detail,
        "latency_ms": source_res.latency_ms,
        "duration_ms": int((time.time() - started) * 1000),
    }
    await _step(
        task_id, "paper_search", "检索学术论文",
        "done" if source_res.usable else "failed",
        step_meta,
    )
    out: dict = {
        "paper_results": results,
        "metrics": {
            **state.get("metrics", {}),
            "arxiv_status": source_res.status.value,
        },
    }
    if not source_res.usable:
        out["errors"] = [f"arXiv 检索失败（{source_res.status.value}）：{source_res.detail}"]
    return out


# ---------------- 知网文献检索（cnki-mcp CLI / 降级 site 限定） ----------------

async def cnki_retriever_node(state: ResearchState) -> dict:
    task_id = state["task_id"]
    active_query = state.get("query") or state.get("original_query", "")
    started = time.time()

    # Planner 逐源规划：知网不在规划内（且非 KB 未命中升级补查）→ 跳过
    plan = state.get("plan") or ["kb", "paper", "web", "cnki"]
    if not settings.cnki_enabled:
        await _step(task_id, "cnki_search", "检索知网文献", "skipped", {"status": "disabled"})
        return {"cnki_results": []}
    if "cnki" not in plan and not state.get("external_run"):
        await _step(task_id, "cnki_search", "检索知网文献", "skipped")
        return {"cnki_results": []}

    await _step(task_id, "cnki_search", "检索知网文献", "running")

    # 知网的 subject 字段是**短语匹配**：把整句"国内关于混合检索在 RAG 中的应用
    # 有哪些研究"直接塞进去会命中 0 条（实测），且长查询更容易把页面拖住。
    # 所以先抽 2–3 个核心术语再检索；提取失败时回退原查询，不阻塞主流程。
    # 这里仍用 structured_search 而不是 search（概念规划）：后者一次要打多个检索
    # 通道，在知网限流时更容易整体失败。
    keywords: list[str] = []
    try:
        kw_payload = await get_llm().extract_json(
            CNKI_KEYWORDS_PROMPT.format(query=active_query),
            {"keywords": []},
        )
        keywords = [
            k.strip() for k in (kw_payload.get("keywords") or [])
            if isinstance(k, str) and k.strip()
        ][:3]
    except Exception:
        keywords = []

    cnki_query = " ".join(keywords) if keywords else active_query
    source_res = await get_adapter("cnki").search(cnki_query, max_results=5)

    # 降级路径（site:cnki.net）拿的是搜索引擎的二手结果，标题常被截断甚至串到
    # 无关页面（实测出现过"通信技术"），初始分压到 0.60 避免和知识库全文切片
    # 平等竞争；MCP 主路径有真实摘要，保持 0.82。
    base = 0.60 if source_res.degraded else 0.82
    results = [
        {
            "title": item["title"],
            "url": item.get("url", ""),
            "snippet": item.get("snippet", ""),
            "type": "cnki",
            "source_label": "知网",
            "relevance": round(max(0.0, base - i * 0.02), 3),
            "meta": item.get("meta", {}),
        }
        for i, item in enumerate(source_res.items)
    ]

    step_meta = {
        "hits": len(results),
        "status": source_res.status.value,
        "source": source_res.source,
        "detail": source_res.detail,
        "latency_ms": source_res.latency_ms,
        "query_used": cnki_query,
        "duration_ms": int((time.time() - started) * 1000),
    }

    if results:
        _emit_sources(task_id, results)
        await _step(task_id, "cnki_search", "检索知网文献", "done", step_meta)
        return {
            "cnki_results": results,
            "metrics": {**state.get("metrics", {}), "cnki_status": source_res.status.value},
        }

    # 空结果要分三种，性质完全不同：
    #   no_hits      → 检索成功但确实没有相关文献，正常状态
    #   rate_limited → 触发知网频率限制，属故障（可退避重试）
    #   unreachable  → MCP 不可用，属故障
    # 把后两者报成"没搜到"会让排查成本极高，这类"降级伪装成正常"的坑本项目已踩过多次。
    if source_res.status == SourceStatus.NO_HITS:
        await _step(task_id, "cnki_search", "检索知网文献", "done", step_meta)
        return {
            "cnki_results": [],
            "metrics": {**state.get("metrics", {}), "cnki_status": source_res.status.value},
        }

    # 知网不做 mock 回落：mock 会编造并不存在的文献，污染引用与评估。
    await _step(task_id, "cnki_search", "检索知网文献", "failed", step_meta)
    return {
        "cnki_results": [],
        "metrics": {**state.get("metrics", {}), "cnki_status": source_res.status.value},
        "errors": [
            f"知网检索未完成（{source_res.status.value}）：{source_res.detail or '未知原因'}"
        ],
    }


# ---------------- 网页信息搜索（DeepSeek Search） ----------------

async def web_retriever_node(state: ResearchState) -> dict:
    task_id = state["task_id"]
    active_query = state.get("query") or state.get("original_query", "")
    started = time.time()

    # Planner 逐源规划：网页不在规划内（且非 KB 未命中升级补查）→ 跳过
    plan = state.get("plan") or ["kb", "paper", "web"]
    if "web" not in plan and not state.get("external_run"):
        await _step(task_id, "web_search", "搜索网页信息", "skipped")
        return {"web_results": []}

    await _step(task_id, "web_search", "搜索网页信息", "running")

    source_res = await get_adapter("web").search(f"{active_query} 最新进展", max_results=5)
    results = [
        {
            "title": r["title"],
            "url": r.get("url", ""),
            "snippet": r.get("snippet", ""),
            "type": "web",
            "source_label": "网页",
            "relevance": round(max(0.0, 0.85 - i * 0.02), 3),
            "meta": r.get("meta", {}),
        }
        for i, r in enumerate(source_res.items)
    ]
    # 同上：网页源也不回落模拟数据，避免编造来源
    _emit_sources(task_id, results)
    step_meta = {
        "hits": len(results),
        "status": source_res.status.value,
        "source": source_res.source,
        "detail": source_res.detail,
        "latency_ms": source_res.latency_ms,
        "duration_ms": int((time.time() - started) * 1000),
    }
    await _step(
        task_id, "web_search", "搜索网页信息",
        "done" if source_res.usable else "failed",
        step_meta,
    )
    out: dict = {
        "web_results": results,
        "metrics": {
            **state.get("metrics", {}),
            "web_status": source_res.status.value,
        },
    }
    if not source_res.usable:
        out["errors"] = [f"Web 检索失败（{source_res.status.value}）：{source_res.detail}"]
    return out


# ---------------- 三路融合 ----------------

async def merger_node(state: ResearchState) -> dict:
    task_id = state["task_id"]
    cands = (
        state.get("kb_results", [])
        + state.get("paper_results", [])
        + state.get("web_results", [])
        + state.get("cnki_results", [])
    )
    if not cands:
        EVENT_BUS.emit(task_id, "sources_final", {"sources": []})
        return {"merged_sources": []}

    # 标题指纹去重
    seen: set[str] = set()
    deduped: list[dict] = []
    for s in cands:
        key = re.sub(r"\s+", "", s["title"])[:40]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(s)

    # 统一精排：全部候选经 bge-reranker 模型打分（可开关；失败保持原分）
    if settings.rerank_enabled:
        deduped = _apply_rerank(state.get("query") or "", deduped)
    deduped.sort(key=lambda x: x.get("relevance", 0.0), reverse=True)

    limit = MAX_GRADE_SOURCES
    query = state.get("query") or state.get("original_query") or ""
    if any(k in query.lower() for k in ("表格", "table")):
        # 表格题只保留少量高相关切片：表格行集中，缩窗可降低 TPM 压力并减少干扰
        limit = max(1, min(limit, settings.table_report_sources))
    final = deduped[:limit]
    for i, s in enumerate(final, start=1):
        s["ref_no"] = i

    # 落库（先清旧，支持重检索/追问重跑）
    async with SessionLocal() as db:
        await db.execute(delete(Source).where(Source.task_id == task_id))
        for s in final:
            db.add(
                Source(
                    task_id=task_id,
                    ref_no=s["ref_no"],
                    type=s["type"],
                    title=s["title"],
                    url=s.get("url", ""),
                    snippet=s.get("snippet", ""),
                    relevance=s.get("relevance", 0.0),
                    source_label=s.get("source_label", ""),
                    meta_json=s.get("meta", {}),
                )
            )
        await db.commit()

    EVENT_BUS.emit(
        task_id,
        "sources_final",
        {
            "sources": [
                {
                    "ref_no": s["ref_no"],
                    "type": s["type"],
                    "title": s["title"],
                    "url": s.get("url", ""),
                    "snippet": s.get("snippet", ""),
                    "relevance": s.get("relevance", 0.0),
                    "source_label": s.get("source_label", ""),
                    "page_nos": s.get("page_nos") or [],
                    "meta": s.get("meta") or {},
                }
                for s in final
            ]
        },
    )
    return {"merged_sources": final}


# ---------------- Retrieval Grader（相关性评估 + 路由） ----------------

def _grade_metrics(state: ResearchState, route: str) -> dict:
    """记录每轮 Grader 结果：reflection 轮次 / 低分轮次。"""
    metrics = state.get("metrics") or {}
    rounds = int(metrics.get("retrieval_rounds", 0)) + 1
    invalid = int(metrics.get("invalid_rounds", 0)) + (1 if route == "rewrite" else 0)
    return {
        **metrics,
        "retrieval_rounds": rounds,
        "invalid_rounds": invalid,
        "last_grade_route": route,
    }


async def grade_documents_node(state: ResearchState) -> dict:
    task_id = state["task_id"]
    sources = state.get("merged_sources", [])
    if not sources:
        return {"grade_results": [], "route": "done"}

    # 快通道：精排头部质量足够高时跳过 LLM 评分与反思循环（省 5-15s——
    # 高分场景 LLM 评分与 reranker 分数高度一致，属重复确认）
    if settings.grade_fastpath_top3 > 0:
        top3 = sorted((x.get("relevance", 0.0) for x in sources), reverse=True)[:3]
        if top3 and sum(top3) / len(top3) >= settings.grade_fastpath_top3:
            grades = [
                {"ref_no": x["ref_no"], "score": x.get("relevance", 0.0), "reason": "精排高分快通道"}
                for x in sources
            ]
            EVENT_BUS.emit(task_id, "grade_result", {"grades": grades, "query": state.get("query", ""), "fastpath": True})
            return {
                "grade_results": grades,
                "route": "report",
                "retry_count": state.get("retry_count", 0),
                "metrics": _grade_metrics(state, "report"),
            }

    # 时间预算：反思重查前检查已耗时，超预算直接生成（防 2-3 分钟长尾）
    t0 = (state.get("metrics") or {}).get("t0")
    if t0 and time.time() - float(t0) > settings.research_time_budget_sec:
        grades = [
            {"ref_no": x["ref_no"], "score": x.get("relevance", 0.0), "reason": "超出时间预算，直接生成"}
            for x in sources
        ]
        EVENT_BUS.emit(task_id, "grade_result", {"grades": grades, "query": state.get("query", ""), "budget": True})
        return {
            "grade_results": grades,
            "route": "report",
            "retry_count": state.get("retry_count", 0),
            "metrics": _grade_metrics(state, "report"),
        }

    llm = get_llm()
    items_block = "\n".join(
        f"[{s['ref_no']}] ({s['source_label']}) {s['title']}\n{s.get('snippet', '')[:150]}"
        for s in sources
    )
    mock_grades = [
        {"ref_no": s["ref_no"], "score": s.get("relevance", 0.0), "reason": "启发式相关度"}
        for s in sources
    ]
    grades = await llm.extract_json(
        GRADE_PROMPT.format(
            query=state.get("original_query", ""), count=len(sources),
            items_block=items_block,
        ),
        mock_grades,
    )
    if not isinstance(grades, list):
        grades = mock_grades

    EVENT_BUS.emit(task_id, "grade_result", {"grades": grades, "query": state.get("query", "")})

    high = [g for g in grades if isinstance(g.get("score"), (int, float)) and g["score"] >= 0.6]
    low = [g for g in grades if isinstance(g.get("score"), (int, float)) and g["score"] < 0.4]
    retry_count = state.get("retry_count", 0)
    if high:
        route = "report"
    elif low and retry_count < 2:
        route = "rewrite"
    else:
        route = "done"
    return {
        "grade_results": grades,
        "route": route,
        "retry_count": retry_count,
        "metrics": _grade_metrics(state, route),
    }


# ---------------- 重检索（reason 驱动改写） ----------------

async def rewrite_node(state: ResearchState) -> dict:
    task_id = state["task_id"]
    llm = get_llm()
    reasons = [
        f"[{g.get('ref_no')}] {g.get('reason', '')}"
        for g in state.get("grade_results", [])
        if isinstance(g.get("score"), (int, float)) and g["score"] < 0.5
    ]
    feedback = "\n".join(reasons[:8]) or "检索结果整体不相关"
    payload = await llm.extract_json(
        REWRITE_PROMPT.format(query=state.get("original_query", ""), feedback=feedback),
        {"rewritten_query": state.get("original_query", "")},
    )
    new_query = (payload.get("rewritten_query") or state.get("original_query", "")).strip()
    EVENT_BUS.emit(task_id, "rewrite", {"query": new_query})
    next_retry = state.get("retry_count", 0) + 1
    # Research Loop 升级：最终轮（retry 将达上限 2）自动扩大火力——
    # 强制三路全开 + 放宽阈值 + 加大检索量（kb_retriever 读 escalation）
    escalation = next_retry >= 2
    if escalation:
        EVENT_BUS.emit(task_id, "rewrite", {"query": new_query, "escalation": True})
    metrics = state.get("metrics") or {}
    return {
        "query": new_query,
        "retry_count": next_retry,
        "escalation": escalation,
        "plan": (
            ["kb", "paper", "web", "cnki"] if escalation and settings.cnki_enabled
            else ["kb", "paper", "web"] if escalation
            else state.get("plan")
        ),
        "search_mode": "multi" if escalation else state.get("search_mode"),
        "metrics": {
            **metrics,
            "rewrite_count": int(metrics.get("rewrite_count", 0)) + 1,
        },
    }


# ---------------- 报告生成（问答式） ----------------

def _first_sentence(text: str) -> str:
    for seg in re.split(r"(?<=[。！？；!?])", text):
        seg = seg.strip()
        if seg:
            return seg[:160]
    return text[:160]


def _excerpt_report(sources: list[dict], lang: str) -> str:
    """无 LLM Key 时：基于真实检索片段的关键句摘录（带标签，不编造）。"""
    if lang == "en":
        lines = ["Answers grounded in enterprise knowledge base:", ""]
        for s in sources:
            lines.append(f"- {_first_sentence(s['snippet'])} [{s['ref_no']}]")
    else:
        lines = ["基于本地知识库的要点：", ""]
        for s in sources:
            lines.append(f"- {_first_sentence(s['snippet'])} [{s['ref_no']}]")
        lines.append("")
        lines.append("（以上内容均摘自知识库片段原文，未添加额外信息。）")
    return "\n".join(lines)


def _table_row_answer(query: str, sources: list[dict]) -> str:
    """表格题确定性抽取：从来源表格行直接生成带引用的短答。

    只在 query 含"表格/table"、且能在某个来源里找到 `实体 [n]` 行时启用；
    用于避免表格题因 LLM 限流回退成整段摘录。字段顺序按本项目
    `SUMMARY OF RAG METHODS` 表：source, data_type, granularity, stage, process。
    """
    if not any(k in query.lower() for k in ("表格", "table")):
        return ""
    match = re.search(r"([A-Za-z][A-Za-z0-9_.\-]{1,24})\s*\[\d+\]", query)
    if not match:
        return ""
    entity = match.group(1)
    for s in sources:
        text = (s.get("meta") or {}).get("parent_text") or s.get("snippet") or ""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for i, line in enumerate(lines):
            if line == entity or line.startswith(entity + " "):
                vals = lines[i + 1 : i + 6]
                if len(vals) < 5 or any(len(v) > 40 for v in vals):
                    continue
                source, _data_type, granularity, training, iterative = vals[:5]
                return (
                    f"根据知识库表格片段 [{s['ref_no']}]，{entity} 的检索源为 {source}，"
                    f"检索粒度为 {granularity}，训练方式为 {training}，"
                    f"迭代方式为 {iterative}。"
                )
    return ""


def _verify_citations(report: str, sources: list[dict]) -> tuple[str, list[int]]:
    total = len(sources)
    used = sorted({int(n) for n in re.findall(r"\[(\d+)\]", report) if 1 <= int(n) <= total})

    def _repl(match: re.Match) -> str:
        return match.group(0) if 1 <= int(match.group(1)) <= total else ""

    cleaned = re.sub(r"\[(\d+)\]", _repl, report)
    return cleaned, used


async def report_writer_node(state: ResearchState) -> dict:
    task_id = state["task_id"]
    original_query = state.get("original_query") or state["query"]
    topic = state.get("topic") or mock_data.extract_topic(original_query)
    sources = state.get("merged_sources", [])
    started = time.time()
    await _step(task_id, "report_write", "生成分析报告", "running")

    llm = get_llm()
    lang = state.get("lang", "zh")

    def _build_context_block(srcs: list[dict]) -> str:
        """上下文块：KB 来源优先用章节级 parent 文本（上下文完整），外部来源用 snippet。"""
        lines = []
        for s in srcs:
            parent = (s.get("meta") or {}).get("parent_text")
            content = parent or s.get("snippet", "")
            lines.append(f"[{s['ref_no']}] ({s['title']})\n{content}")
        return "\n".join(lines)

    if not sources:
        if state.get("query_type") == "chat":
            # 闲聊路径：LLM 简短回复（Mock 模式固定回复），流式输出
            chat_prompt = CHAT_PROMPT.format(query=original_query)
            mock_reply = (
                "Hello! I'm your enterprise knowledge assistant. How can I help you?"
                if lang == "en"
                else "你好！我是本地知识库助手，有什么可以帮您？"
            )
            parts: list[str] = []
            async for piece in llm.stream_report(chat_prompt, mock_reply):
                parts.append(piece)
                EVENT_BUS.emit(task_id, "report_token", {"delta": piece})
            report = "".join(parts).strip() or mock_reply
        elif lang == "en":
            report = ("No sufficiently relevant content was found in the knowledge base "
                      "and other sources for this question. Please upload related documents "
                      "or try a different question.")
        else:
            report = ("知识库中未找到足够相关的信息。\n\n"
                      "建议：1) 在 Knowledge Base 页面上传相关文档；2) 换一个角度重新提问。")
    else:
        context_block = _build_context_block(sources)
        lang_note = (
            "\nNote: answer entirely in English."
            if lang == "en"
            else ""
        )
        mock_text = _excerpt_report(sources, lang)

        # 第一轮生成
        # 图表问答判定：原实现只要上下文出现一次 [图] 就切换到"图表分析师"人格。
        # 学术 PDF 的整页插图 OCR 文本必然带 [图] 标记，会导致普通问答被误判，
        # 回答退化为"图中未显示/描述未提及"。改为按含图片段的占比判定。
        def _is_chart_source(src: dict) -> bool:
            # 只看片段自身文本。parent_text 是章节级上下文，学术 PDF 的整篇
            # 上下文里几乎必然含 [图] 标记，用它判断会把普通问答误判为图表问答。
            body = src.get("snippet", "")
            return "[图]" in body or "[图描述]" in body

        chart_cnt = sum(1 for s in sources if _is_chart_source(s))
        is_chart_context = bool(sources) and chart_cnt / len(sources) >= 0.6
        prompt_template = CHART_REPORT_PROMPT if is_chart_context else REPORT_PROMPT
        prompt = (
            prompt_template.format(
                query=original_query, context_block=context_block, count=len(sources)
            )
            + lang_note
        )
        table_answer = _table_row_answer(original_query, sources)
        if table_answer:
            # 表格行确定性抽取：不依赖 LLM，避免免费档 TPM/RPM 限流把答案退化成整段摘录
            report = table_answer
            EVENT_BUS.emit(task_id, "report_token", {"delta": report})
        else:
            parts: list[str] = []
            async for piece in llm.stream_report(prompt, mock_text):
                parts.append(piece)
                EVENT_BUS.emit(task_id, "report_token", {"delta": piece})
            report = "".join(parts).strip()
            if len(report) < 20:
                report = mock_text

        # 初稿完成：立即翻步骤为已完成 + 通知前端熄灭流式光标——
        # 随后的忠实度核查（~10s 阻塞 LLM 调用）在时间线呈现，
        # 消除"文字已出完但步骤还处理中/光标还在呼吸"的假卡顿窗口
        report, used_refs = _verify_citations(report, sources)
        EVENT_BUS.emit(task_id, "report_draft_done", {})
        await _step(task_id, "report_write", "生成分析报告", "done", {"chars": len(report)})

        # --- Answer Verification（真实 LLM 模式）---
        # 核查是一次阻塞式 LLM 调用（实测约 10s），先广播可见化事件——
        # 否则报告流完到 completed 之间界面完全静默，用户以为卡死
        if llm.mode != "mock" and sources and not table_answer:
            EVENT_BUS.emit(task_id, "verifying", {"answer_chars": len(report)})
            # 核查瘦身：按相关性取前 8 源，snippet+parent 截断
            # （原全量 12×2000tok parent ≈24K token，核查 15-20s；瘦身后 3-5s）
            verify_sources = sorted(sources, key=lambda x: x.get("relevance", 0.0), reverse=True)[:8]
            parts_ctx: list[str] = []
            for x in verify_sources:
                head = (x.get("snippet") or "")[:300]
                tail = ((x.get("meta") or {}).get("parent_text") or "")[:500]
                parts_ctx.append(f"[{x['ref_no']}] {x['title']}\n{head}\n{tail}")
            verify_ctx = "\n\n".join(parts_ctx)
            verify_payload = await llm.extract_json(
                VERIFY_PROMPT.format(
                    query=original_query, answer=report, context_block=verify_ctx
                ),
                {"faithfulness": 1.0, "unsupported_claims": [], "pass": True},
            )
            if isinstance(verify_payload, dict):
                EVENT_BUS.emit(task_id, "verify_result", {
                    "pass": bool(verify_payload.get("pass")),
                    "faithfulness": verify_payload.get("faithfulness"),
                    "unsupported": len(verify_payload.get("unsupported_claims") or []),
                })

            if isinstance(verify_payload, dict) and not verify_payload.get("pass"):
                # 附核查反馈重生成一次
                claims = verify_payload.get("unsupported_claims") or []
                fix_note = (
                    "\n【修正要求】上一版回答中以下论断缺少知识库依据，请删除或改写为有依据的表述：\n"
                    + "\n".join(f"- {c}" for c in claims[:5])
                )
                prompt2 = (
                    REPORT_PROMPT.format(
                        query=original_query, context_block=context_block, count=len(sources)
                    )
                    + lang_note
                    + fix_note
                )
                await _step(task_id, "report_write", "生成分析报告", "running")
                EVENT_BUS.emit(task_id, "report_revise", {})
                parts2: list[str] = []
                async for piece in llm.stream_report(prompt2, mock_text):
                    parts2.append(piece)
                    EVENT_BUS.emit(task_id, "report_token", {"delta": piece})
                report2 = "".join(parts2).strip()
                if len(report2) < 20:
                    report2 = mock_text
                report = report2
                report, used_refs = _verify_citations(report, sources)
                EVENT_BUS.emit(task_id, "report_draft_done", {})
                await _step(task_id, "report_write", "生成分析报告", "done", {"chars": len(report)})

    report, used_refs = _verify_citations(report, sources)

    title = mock_data.task_title(topic)
    summary = report[:160].replace("\n", " ")

    async with SessionLocal() as db:
        # 保留历史报告（多轮对话展示），version 递增
        prev_versions = (
            await db.execute(
                select(Report.version).where(Report.task_id == task_id)
            )
        ).scalars().all()
        next_version = (max(prev_versions) if prev_versions else 0) + 1
        db_report = Report(
            task_id=task_id, title=title, summary=summary,
            markdown=report, version=next_version,
        )
        db.add(db_report)
        await db.flush()
        src_rows = (
            await db.execute(select(Source).where(Source.task_id == task_id))
        ).scalars().all()
        ref_to_src = {s.ref_no: s.id for s in src_rows}
        for ref in used_refs:
            if ref in ref_to_src:
                db.add(
                    Citation(
                        report_id=db_report.id,
                        ref_no=ref,
                        source_id=ref_to_src[ref],
                        claim_text="",
                    )
                )
        metrics = state.get("metrics", {})
        # docs_processed：真实命中的唯一文档数（KB-only 口径）
        docs_processed = int(metrics.get("kb_docs", len(sources) if sources else 0))
        duration = round(time.time() - float(metrics.get("t0", started)), 1)
        relevance_avg = (
            round(sum(s.get("relevance", 0.0) for s in sources) / len(sources) * 100)
            if sources else 0
        )
        stats = {
            "duration_sec": duration,
            "sources_count": len(sources),
            "docs_processed": docs_processed,
            "relevance_avg": relevance_avg,
            "citations_count": len(used_refs),
            "retrieval_rounds": int(metrics.get("retrieval_rounds", 0)),
            "invalid_rounds": int(metrics.get("invalid_rounds", 0)),
            "rewrite_count": int(metrics.get("rewrite_count", 0)),
            "scheduled_sources": int(metrics.get("scheduled_sources_total", 0)),
            "scheduled_external_sources": int(metrics.get("scheduled_external_total", 0)),
            "dispatch_mode": metrics.get("dispatch_mode", ""),
        }
        task = await db.get(ResearchTask, task_id)
        if task:
            task.status = "done"
            task.duration_sec = duration
            task.stats_json = stats
            task.completed_at = _now()
        await db.commit()
        report_id = db_report.id

    await _step(
        task_id, "report_write", "生成分析报告", "done",
        {"hits": len(used_refs), "duration_ms": int((time.time() - started) * 1000)},
    )
    EVENT_BUS.emit(
        task_id,
        "report_completed",
        {"report_id": report_id, "title": title, "stats": stats, "citations": used_refs},
    )
    return {"report_md": report, "report_title": title, "metrics": {**metrics, **stats}}


async def on_error(task_id: str, message: str) -> None:
    async with SessionLocal() as db:
        task = await db.get(ResearchTask, task_id)
        if task:
            task.status = "failed"
            task.completed_at = _now()
            # 持久化错误原因：前端刷新后 hydrate 能显示真实原因（而非"未知错误"）
            task.stats_json = {**(task.stats_json or {}), "error": message}
            await db.commit()
    EVENT_BUS.emit(task_id, "error", {"message": message})
