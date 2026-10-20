"""命名依赖度消融实验：把文件名从证据里拿掉，看判定掉多少。

要回答的问题：「API 拆解出来的视觉证据，本身够不够支撑判定？」
做法：同一份审计证据跑两遍，唯一变量是 `_image_name` 是否清空。
      差额就是**靠文件名兜底的那部分** —— 也就是命名依赖度。

为什么值得单独做：用户定的原则是「命名只能参考，不能拿它归类」。
但代码里存在 `FILENAME_PRODUCT_HINTS` / `FILENAME_DIRECTION_HINTS` /
`FILENAME_FALLBACK_PRODUCTS` 三条命名路径，实际依赖度**只能实测**，不能靠读代码猜。
（实测 1114 条：去掉文件名后产品落未知 2.2%→7.4%、方向 5.9%→10.2%，
  即 API 证据独立支撑了 92.6% 的产品判定、89.8% 的方向判定。）

复用 replay_audit 的证据构造与分流逻辑，保证与真实运行、与重放台三方一致。

用法：
    python tools/ablation_filename.py --audit <审计.jsonl> [--audit ...] \
        --source <原始素材> --platform internal
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import classify_pure_api as cpa  # noqa: E402
import replay_audit as ra  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="命名依赖度消融实验（离线，不调 API）")
    parser.add_argument("--audit", action="append", required=True, help="审计 JSONL，可多次指定")
    parser.add_argument("--source", help="原始素材目录（用于读图片尺寸，缺失也能跑）")
    parser.add_argument("--platform", default="internal", choices=sorted(cpa.SUPPORTED_PLATFORMS))
    parser.add_argument("--min-confidence", type=float, default=0.85)
    parser.add_argument("--top", type=int, default=15, help="明细最多列几条")
    args = parser.parse_args()

    source = Path(args.source) if args.source else None
    recs = ra.load_audit([Path(p) for p in args.audit])
    print(f"样本 {len(recs)} 条 · 平台 {args.platform} · 唯一变量 = 是否提供文件名\n")

    rows: list[tuple[str, tuple[str, str], tuple[str, str]]] = []
    stat = {"with": Counter(), "without": Counter()}

    for name, rec in recs.items():
        with_name = ra.replay_one(rec, source, args.platform, args.min_confidence)
        blind = dict(rec)
        blind["file"] = ""                       # 唯一的改动：抹掉文件名
        without_name = ra.replay_one(blind, source, args.platform, args.min_confidence)
        if not with_name or not without_name:
            continue

        def pick(r: dict) -> tuple[str, str]:
            direction = str(r.get("direction") or "未知")
            if r.get("output_mode") == "business_line":
                return str(r.get("output_group") or "未知"), direction
            return str(r.get("output_product") or r.get("product") or "未知"), direction

        a, b = pick(with_name), pick(without_name)
        rows.append((name, a, b))
        for tag, val in (("with", a), ("without", b)):
            stat[tag]["n"] += 1
            if val[0] == "未知":
                stat[tag]["prod_unknown"] += 1
            if val[1] == "未知":
                stat[tag]["dir_unknown"] += 1

    n = stat["with"]["n"] or 1
    print("=" * 84)
    print(f"  {'指标':<22}{'有文件名':>16}{'无文件名':>16}{'变化':>12}")
    print("=" * 84)
    for label, key in (("产品落「未知」", "prod_unknown"), ("方向落「未知」", "dir_unknown")):
        a, b = stat["with"][key], stat["without"][key]
        print(f"  {label:<22}{a:>8} ({a / n * 100:4.1f}%){b:>8} ({b / n * 100:4.1f}%){b - a:>+12}")
    print()
    print(f"  → API 证据**独立**支撑了 {(n - stat['without']['prod_unknown']) / n * 100:.1f}% 的产品判定、"
          f"{(n - stat['without']['dir_unknown']) / n * 100:.1f}% 的方向判定")
    print("  → 差额即命名依赖度：产品"
          f" {stat['without']['prod_unknown'] - stat['with']['prod_unknown']} 条、"
          f"方向 {stat['without']['dir_unknown'] - stat['with']['dir_unknown']} 条")
    print()

    prod_flip: Counter = Counter()
    dir_flip: Counter = Counter()
    for _name, a, b in rows:
        if a[0] != b[0]:
            prod_flip[(a[0], b[0])] += 1
        if a[1] != b[1]:
            dir_flip[(a[1], b[1])] += 1

    print("=== 产品判定：去掉文件名后的变化（有 → 无）===")
    for (a, b), v in prod_flip.most_common(args.top):
        print(f"   {v:>4}   {a:<20} → {b}")
    print(f"   —— 共 {sum(prod_flip.values())} 条产品判定依赖文件名")
    print()
    print("=== 方向判定：去掉文件名后的变化（有 → 无）===")
    for (a, b), v in dir_flip.most_common(args.top):
        print(f"   {v:>4}   {a:<18} → {b}")
    print(f"   —— 共 {sum(dir_flip.values())} 条方向判定依赖文件名")
    print()
    print("提示：命中「文件名兜底」的产品应优先补视觉证据（如红果短剧的 Logo 形状），")
    print("      而不是继续依赖命名 —— 这是用户明确要求的方向。")


if __name__ == "__main__":
    main()
