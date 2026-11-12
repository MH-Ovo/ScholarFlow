"""生成端到端评测集（20 条）：从现有 L1 数据集抽样 + 2 条知网场景题。

用法：python eval/scripts/make_e2e_dataset.py
输出：eval/dataset_e2e.json
"""
import json
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[1]

EN_IDS = ["sf01", "sf02", "sf06", "sf13", "sf15", "sf17", "sf19", "sf21", "sf28", "sf29"]
CN_IDS = ["cn04", "cn06", "cn12", "cn14", "cn16", "cn18", "cn22", "cn26"]

CUSTOM = [
    {
        "id": "e2e-cnki-01",
        "question": "国内关于混合检索在 RAG 中的应用有哪些代表性研究？它们的做法有何不同？",
        "reference": (
            "国内代表性工作包括：IHRAG（迭代式混合检索增强生成，动态路由协同向量检索与"
            "知识图谱推理，结合医疗本体查询解构与“检索-验证-迭代”闭环）；HCHRS（面向企业"
            "知识库的层次化分块与双通道混合检索，结构感知分块 + 父子块双层索引 + 交叉重排）；"
            "HyRO（融合向量编码与知识图谱的混合检索框架，两阶段实体消歧、社区摘要与交叉编码器"
            "重排）。三者的差异主要在检索信号（向量/图谱/词法）、分块方式和重排策略。"
        ),
        "type": "comparison",
        "lang": "zh",
        "source": "custom_cnki",
    },
    {
        "id": "e2e-cnki-02",
        "question": "知网上企业知识库场景的 RAG 方案通常如何解决分块与检索问题？",
        "reference": (
            "典型方案采用结构感知的自适应分块（结合 DOM 与 Confluence 语义边界、三级表格"
            "处理），构建层次化父子块索引（文档摘要与细粒度内容双层索引），并在检索阶段使用"
            "双通道混合检索与交叉重排，融合摘要层与内容层优势；代表工作是 HCHRS。"
        ),
        "type": "comparison",
        "lang": "zh",
        "source": "custom_cnki",
    },
]


def _load(name: str) -> dict[str, dict]:
    path = EVAL_DIR / name
    data = json.loads(path.read_text(encoding="utf-8"))
    return {x["id"]: x for x in data}


def _item(x: dict, source: str) -> dict:
    return {
        "id": f"e2e-{x['id']}",
        "source_id": x["id"],
        "question": x["question"],
        "reference": x["ground_truth"],
        "type": x.get("type", "fact"),
        "lang": "zh",
        "source": source,
        "doc_name": x.get("doc_name", ""),
        "relevant_chunks": x.get("relevant_chunks", []),
    }


def main() -> None:
    en = _load("dataset_scholarflow.json")
    cn = _load("dataset_scholarflow_cn.json")

    items = [_item(en[i], "scholarflow_en") for i in EN_IDS]
    items += [_item(cn[i], "scholarflow_cn") for i in CN_IDS]
    items += CUSTOM

    out = EVAL_DIR / "dataset_e2e.json"
    out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out} ({len(items)} items)")
    print("types:", {t: sum(1 for x in items if x["type"] == t) for t in {x["type"] for x in items}})
    print("sources:", {s: sum(1 for x in items if x["source"] == s) for s in {x["source"] for x in items}})


if __name__ == "__main__":
    main()
