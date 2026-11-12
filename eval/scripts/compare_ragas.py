"""对比两份 RAGAS JSON：聚合指标 + 逐条差值。

用法：
    python eval/scripts/compare_ragas.py --base eval/results/ragas_cnki_off_x.json --treat eval/results/ragas_cnki_on_x.json
"""
import argparse
import json
from pathlib import Path


def load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--treat", required=True)
    args = parser.parse_args()

    base, treat = load(args.base), load(args.treat)
    print(f"base : {args.base} tag={base.get('meta', {}).get('tag')}")
    print(f"treat: {args.treat} tag={treat.get('meta', {}).get('tag')}")
    print()

    for layer in ("retrieval", "generation"):
        b, t = base.get(layer), treat.get(layer)
        if not b or not t:
            continue
        print(f"[{layer}]")
        keys = set(b.get("aggregate", {})) | set(t.get("aggregate", {}))
        for key in sorted(keys):
            bv, tv = b["aggregate"].get(key), t["aggregate"].get(key)
            delta = (tv - bv) if isinstance(bv, (int, float)) and isinstance(tv, (int, float)) else None
            print(f"  {key}: {bv} -> {tv}  delta={delta:+.4f}" if delta is not None else f"  {key}: {bv} -> {tv}")
        print()

    # 逐条差值（按 id 对齐）
    def rows(payload: dict, layer: str) -> dict[str, dict]:
        data = (payload.get(layer) or {}).get("items") or []
        return {x.get("id"): x for x in data}

    for layer in ("retrieval", "generation"):
        b, t = rows(base, layer), rows(treat, layer)
        if not b or not t:
            continue
        print(f"[{layer}] per-item delta")
        for item_id in sorted(set(b) & set(t)):
            key = "context_recall" if layer == "retrieval" else "faithfulness"
            bv, tv = b[item_id].get(key), t[item_id].get(key)
            if isinstance(bv, (int, float)) and isinstance(tv, (int, float)) and abs(tv - bv) >= 0.05:
                print(f"  {item_id}: {bv:.3f} -> {tv:.3f} ({tv - bv:+.3f})")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
