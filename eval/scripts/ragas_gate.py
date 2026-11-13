"""RAGAS 回归门禁：读取评测 JSON，低于阈值即非零退出（供 CI/夜间任务使用）。

用法：
    python eval/scripts/ragas_gate.py --input eval/results/ragas_kb140_generation_final_xxx.json
    python eval/scripts/ragas_gate.py --input ... --min-context 0.79 --min-faithfulness 0.91
"""
import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "eval" / "results"


def _latest(pattern: str) -> Path | None:
    files = sorted(glob.glob(str(RESULTS / pattern)))
    return Path(files[-1]) if files else None


def check(payload: dict, min_context: float, min_faith: float) -> tuple[bool, dict]:
    retr = (payload.get("retrieval") or {}).get("aggregate") or {}
    gen = (payload.get("generation") or {}).get("aggregate") or {}
    values = {
        "context_recall_retrieval": retr.get("context_recall"),
        "context_recall_generation": gen.get("context_recall"),
        "faithfulness": gen.get("faithfulness"),
    }
    ok = True
    if values["context_recall_retrieval"] is not None:
        ok = ok and float(values["context_recall_retrieval"]) >= min_context
    if values["faithfulness"] is not None:
        ok = ok and float(values["faithfulness"]) >= min_faith
    return ok, values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="", help="评测 JSON；留空取最新 generation_final")
    parser.add_argument("--min-context", type=float, default=0.79)
    parser.add_argument("--min-faithfulness", type=float, default=0.91)
    args = parser.parse_args()

    path = Path(args.input) if args.input else _latest("ragas_*generation_final_*.json")
    if path is None or not path.exists():
        print(f"[gate] 找不到评测结果：{args.input or 'ragas_*generation_final_*.json'}")
        return 2
    payload = json.loads(path.read_text(encoding="utf-8"))
    ok, values = check(payload, args.min_context, args.min_faithfulness)
    print(f"[gate] {path.name}")
    for k, v in values.items():
        print(f"  {k} = {v}")
    print(f"  thresholds: context>={args.min_context} faithfulness>={args.min_faithfulness}")
    if not ok:
        print("[gate] FAIL")
        return 1
    print("[gate] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
