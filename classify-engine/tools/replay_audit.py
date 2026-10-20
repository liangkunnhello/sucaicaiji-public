"""规则重放台：用已存的审计证据重跑当前本地规则，不调用 API、不动缓存。

为什么必须有这个：改规则最怕"改完不知道有没有变好"。有这个就能在几秒内
对比改动前后的全局影响（今天靠它把产品准确率从 91.0% 推到 97.3%）。

⚠️ 两个必须注意的点（今天都踩过）：
1. **分流逻辑必须和 main() 保持一致**：外部平台走 output_mode=business_line，
   内部系统才套 apply_collection_taxonomy。否则重放结果和真实运行对不上。
2. **按文件名去重、后读覆盖先读**：待确认的图在修复后是改名归档再重跑的，
   旧路径与新路径会各留一条，用 source_key 去重会把同一张图算成两张。

用法：
    # 改规则前先跑一次基线
    python tools/replay_audit.py --audit <审计.jsonl> --source <原始素材> --dump before.json
    # 改完规则再跑，直接看迁移矩阵
    python tools/replay_audit.py --audit <审计.jsonl> --source <原始素材> \
        --dump after.json --compare before.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import classify_pure_api as cpa  # noqa: E402


def load_audit(paths: list[Path]) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for path in paths:
        if not path.is_file():
            print(f"! 审计文件不存在，跳过：{path}")
            continue
        for line in path.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "_待确认-已由修复版解决" in str(rec.get("source", "")):
                continue
            merged[rec.get("file") or str(len(merged))] = rec
    return merged


def replay_one(rec: dict, source: Path | None, platform: str, min_confidence: float) -> dict | None:
    raw = rec.get("api_response_json") or rec.get("keywords")
    if not isinstance(raw, dict):
        return None
    evidence = dict(raw)
    evidence["_cached"] = True
    evidence["_image_name"] = rec.get("file", "")
    if source:
        img = source / str(rec.get("file", ""))
        if img.is_file():
            width, height = cpa.read_image_dimensions(img)
            evidence["_image_width"] = width
            evidence["_image_height"] = height
    evidence["_api_response_raw"] = str(rec.get("api_response_raw") or "")
    if isinstance(rec.get("api_meta"), dict):
        evidence["_api_meta"] = rec["api_meta"]

    classified = cpa.classify_keyword_evidence(evidence, min_confidence, platform)
    # 和 main() 一致：按引擎自己声明的输出模式分流
    if classified.get("output_mode") == "business_line":
        result = classified
    else:
        result = cpa.apply_collection_taxonomy(classified)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="规则重放台（离线，不调 API）")
    parser.add_argument("--audit", action="append", required=True, help="审计 JSONL，可多次指定")
    parser.add_argument("--source", help="原始素材目录（用于读图片尺寸，缺失也能跑）")
    parser.add_argument("--platform", default="internal",
                        choices=sorted(cpa.SUPPORTED_PLATFORMS))
    parser.add_argument("--min-confidence", type=float, default=0.85)
    parser.add_argument("--dump", help="把逐条结果写成 JSON，供 --compare 用")
    parser.add_argument("--compare", help="与上一次的 dump 对比，输出迁移矩阵")
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    recs = load_audit([Path(p) for p in args.audit])
    if not recs:
        print("没有可重放的记录")
        sys.exit(1)
    source = Path(args.source) if args.source else None

    results: dict[str, dict] = {}
    for name, rec in recs.items():
        try:
            out = replay_one(rec, source, args.platform, args.min_confidence)
        except Exception as exc:  # noqa: BLE001
            print(f"! 重放失败 {name}: {exc}")
            continue
        if out is None:
            continue
        results[name] = {
            "product": str(out.get("product")),
            "direction": str(out.get("direction")),
            "group": str(out.get("output_group")),
            "level": str(out.get("classification_level")),
            "mode": str(out.get("output_mode")),
            "detected_product": str(out.get("detected_product")),
            "detected_direction": str(out.get("detected_direction")),
        }

    total = len(results)
    print(f"\n{'=' * 72}\n重放 {total} 条 · 平台 {args.platform} · 阈值 {args.min_confidence}\n{'=' * 72}")
    print(f"  输出模式: {dict(Counter(v['mode'] for v in results.values()))}")
    print(f"  完整度  : {dict(Counter(v['level'] for v in results.values()))}")
    print(f"  一级分类: {dict(Counter(v['group'] for v in results.values()).most_common(args.top))}")
    print(f"  产品    : {dict(Counter(v['product'] for v in results.values()).most_common(args.top))}")
    print(f"  方向    : {dict(Counter(v['direction'] for v in results.values()).most_common(args.top))}")
    unknown_dir = sum(1 for v in results.values() if v["direction"] == "未知")
    unknown_prod = sum(1 for v in results.values() if v["product"] in ("未知", "待确认"))
    print(f"  方向未知: {unknown_dir} ({unknown_dir / max(total, 1) * 100:.1f}%)"
          f"   产品未知: {unknown_prod} ({unknown_prod / max(total, 1) * 100:.1f}%)")

    if args.dump:
        Path(args.dump).write_text(
            json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  → 已写出 {args.dump}")

    if args.compare:
        prev_path = Path(args.compare)
        if not prev_path.is_file():
            print(f"  ! 对比文件不存在：{prev_path}")
        else:
            prev = json.loads(prev_path.read_text(encoding="utf-8"))
            changed = [
                (name, prev[name], now)
                for name, now in results.items()
                if name in prev and (prev[name]["product"] != now["product"]
                                     or prev[name]["direction"] != now["direction"])
            ]
            print(f"\n{'-' * 72}\n与 {prev_path.name} 的差异：{len(changed)} 条判定变化\n{'-' * 72}")
            for (was, now), n in Counter(
                (f"{p['product']}/{p['direction']}", f"{c['product']}/{c['direction']}")
                for _, p, c in changed
            ).most_common(args.top):
                print(f"  {n:>5}  {was:<26} →  {now}")
            if not changed:
                print("  ✓ 判定完全一致")


if __name__ == "__main__":
    main()
