"""LangGraph 装配（Planner 逐源规划 + SendAPI 动态多源路由 + Self-RAG 反思）。

START → router
        ├─ chat ─────────────→ report_write → END
        ├─ kb_only ──────────→ kb_search
        │                        ├─ 命中 → merger
        │                        └─ 未命中 → fanout_external → 外部源（SendAPI）
        └─ multi ────────────→ fanout → SendAPI 按 plan 分发 kb/paper/web/cnki
                                 → merger → grade
                                     ├─ 高分 → report_write → END
                                     └─ 低分且 retry<2 → rewrite → 回 fanout/kb_search
                                     └─ 重试耗尽 → report_write

关键点：
- 检索源节点从 `source_registry.SOURCE_SPECS` 注册，graph 不再写死边；
- dynamic_routing=true 时用 `Send` 只分发 Planner 规划的源；
- dynamic_routing=false 时保留旧固定扇出，用于 A/B 对照。
"""
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.agents import nodes
from app.agents.source_registry import SOURCE_SPECS, source_node_names
from app.agents.state import ResearchState
from app.core.config import get_settings


def route_after_router(state: ResearchState):
    if state.get("query_type") == "chat":
        return "chat"
    if state.get("search_mode") == "multi":
        return "multi"
    return "kb_only"


def route_after_kb(state: ResearchState) -> str:
    """KB 命中 → 融合；未命中 → 外部源升级；multi 模式直接融合。"""
    if "kb" not in (state.get("plan") or ["kb"]):
        return "merge"
    if state.get("search_mode") == "multi":
        return "merge"
    return "hit" if state.get("kb_results") else "miss"


def route_after_rewrite(state: ResearchState) -> str:
    if state.get("escalation") or state.get("search_mode") == "multi":
        return "multi"
    return "kb_only"


def route_after_grade(state: ResearchState) -> str:
    return state.get("route", "report")


def _send_sources(state: ResearchState, include_kb: bool = True) -> list[Send]:
    """SendAPI 动态分发：只给 Planner 规划且已注册的源发消息。"""
    plan = state.get("plan") or ["kb"]
    specs = [
        spec for key, spec in SOURCE_SPECS.items()
        if key in plan and (include_kb or key != "kb")
    ]
    return [Send(spec.node, state) for spec in specs]


def route_sources(state: ResearchState) -> list[Send]:
    return _send_sources(state, include_kb=True)


def route_external_sources(state: ResearchState) -> list[Send]:
    """KB 未命中升级：只补外部源，避免重复跑 KB。"""
    return _send_sources(state, include_kb=False)


def build_graph(dynamic: bool = True):
    g = StateGraph(ResearchState)

    g.add_node("router", nodes.router_node)
    g.add_node("fanout", nodes.fanout_node)
    g.add_node("fanout_external", nodes.fanout_external_node)
    g.add_node("merger", nodes.merger_node)
    g.add_node("grade", nodes.grade_documents_node)
    g.add_node("rewrite", nodes.rewrite_node)
    g.add_node("report_write", nodes.report_writer_node)

    # 所有检索源从注册表自动加节点；新增源不用改 graph 的 add_node/边列表
    for spec in SOURCE_SPECS.values():
        g.add_node(spec.node, getattr(nodes, spec.func))

    g.add_edge(START, "router")
    g.add_conditional_edges(
        "router",
        route_after_router,
        {"chat": "report_write", "kb_only": "kb_search", "multi": "fanout"},
    )

    if dynamic:
        # SendAPI：fanout 节点本身只记账，真正的分支由 route_sources 返回 Send
        g.add_conditional_edges("fanout", route_sources, source_node_names())
        g.add_conditional_edges(
            "fanout_external", route_external_sources, source_node_names()
        )
    else:
        # 旧固定扇出：保留给 A/B 对照
        for spec in SOURCE_SPECS.values():
            g.add_edge("fanout", spec.node)
        for spec in SOURCE_SPECS.values():
            if spec.key != "kb":
                g.add_edge("fanout_external", spec.node)

    g.add_conditional_edges(
        "kb_search",
        route_after_kb,
        {"hit": "merger", "miss": "fanout_external", "merge": "merger"},
    )

    for spec in SOURCE_SPECS.values():
        if spec.key != "kb":
            g.add_edge(spec.node, "merger")

    g.add_edge("merger", "grade")
    g.add_conditional_edges(
        "grade",
        route_after_grade,
        {"report": "report_write", "rewrite": "rewrite", "done": "report_write"},
    )
    g.add_conditional_edges(
        "rewrite",
        route_after_rewrite,
        {"kb_only": "kb_search", "multi": "fanout"},
    )
    g.add_edge("report_write", END)
    return g.compile()


_GRAPHS: dict[bool, object] = {}


def get_research_graph(dynamic: bool | None = None):
    if dynamic is None:
        dynamic = bool(get_settings().dynamic_routing)
    if dynamic not in _GRAPHS:
        _GRAPHS[dynamic] = build_graph(dynamic)
    return _GRAPHS[dynamic]
