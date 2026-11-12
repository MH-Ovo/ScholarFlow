"""可注册的检索源注册表（SendAPI 动态路由的数据源）。

新增一个源：
1. 实现 `async def xxx_retriever_node(state) -> dict`；
2. `register_source(SourceSpec(key="xxx", node="xxx_search", label="..."))`；
3. 在 `merger_node` 合并该源的 result_key。
LangGraph 会自动把注册表里的 node 加入图，`SendAPI` 也按注册表动态分发。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceSpec:
    key: str          # planner 使用的 key：kb/paper/web/cnki
    node: str         # LangGraph 节点名
    func: str         # nodes.py 里的函数名
    label: str        # 展示名
    result_key: str   # ResearchState 里的结果字段


SOURCE_SPECS: dict[str, SourceSpec] = {
    "kb": SourceSpec("kb", "kb_search", "kb_retriever_node", "本地知识库", "kb_results"),
    "paper": SourceSpec("paper", "paper_search", "paper_retriever_node", "学术论文", "paper_results"),
    "web": SourceSpec("web", "web_search", "web_retriever_node", "网页信息", "web_results"),
    "cnki": SourceSpec("cnki", "cnki_search", "cnki_retriever_node", "知网文献", "cnki_results"),
}


def register_source(spec: SourceSpec) -> None:
    SOURCE_SPECS[spec.key] = spec


def unregister_source(key: str) -> None:
    SOURCE_SPECS.pop(key, None)


def source_node_names() -> list[str]:
    return [s.node for s in SOURCE_SPECS.values()]


def spec_for_node(node: str) -> SourceSpec | None:
    return next((s for s in SOURCE_SPECS.values() if s.node == node), None)
