"""规则健康报告：基于已有审计文件，自动做归因分析，不调 API。

今天（2026-09-21）我手工做的三件事，都应该由这个脚本自动完成：

1. 门控归因 —— 从 reason 里解析「分类表门控：源产品/源方向 → 组/产品/方向」，
   统计"源方向本来有值、却被门控成未知"的 TOP N。今天 110 条就是这么找出来的。
2. 零命中词 —— 规则表里每条证据词，在全部审计语料里命中多少次。
   零命中说明它要么是死词表（模型永远不这么写），要么这批素材用不上。
3. 新方向候选 —— 模型给出的、但不在任何产品允许集里的方向，按出现次数排序。
   这是「方向不匹配」的客观依据，也是未来晋升机制的输入。

用法：
    python tools/rule_health.py --audit <审计.jsonl> [--audit <再来一个>]
    python tools/rule_health.py --audit <审计.jsonl> --source <原始素材目录>   # 附带与文件名的对比
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import classify_pure_api as cpa  # noqa: E402

GATE_RE = re.compile(r"分类表门控：([^/→]+)/([^ ]+?)\s*→\s*([^/]+)/([^/]+)/(.+)$")


def load_audits(paths: list[Path]) -> dict[str, dict]:
    """按文件名去重，后读的覆盖先读的（同一张图重跑时取最新结果）。"""
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


def corpus_of(rec: dict) -> str:
    """复现引擎看到的语料，保证零命中统计和打分口径一致。"""
    raw = rec.get("api_response_json") or rec.get("keywords")
    if not isinstance(raw, dict):
        # 拿不到结构化证据时退回 keywords 原文，尽量别漏
        raw = rec.get("keywords") if isinstance(rec.get("keywords"), dict) else {}
    try:
        return cpa.keyword_corpus(cpa.normalize_keyword_evidence(raw))
    except Exception:  # noqa: BLE001
        return ""


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main() -> None:
    parser = argparse.ArgumentParser(description="规则健康报告")
    parser.add_argument("--audit", action="append", required=True, help="审计 JSONL，可多次指定")
    parser.add_argument("--source", help="原始素材目录，用于与文件名的历史标签对比")
    parser.add_argument("--top", type=int, default=15, help="各榜单显示条数")
    args = parser.parse_args()

    recs = list(load_audits([Path(p) for p in args.audit]).values())
    if not recs:
        print("没有可分析的审计记录")
        sys.exit(1)
    total = len(recs)
    corpora = {id(r): corpus_of(r) for r in recs}

    # ---------------------------------------------------------------- 概览
    section(f"一、概览（{total} 条）")
    print(f"  status              : {dict(Counter(r.get('status') for r in recs))}")
    print(f"  classification_level: {dict(Counter(r.get('classification_level') for r in recs))}")
    print(f"  platform            : {dict(Counter(str(r.get('platform')) for r in recs))}")
    print(f"  输出模式            : {dict(Counter(str(r.get('output_mode')) for r in recs))}")
    unclassified = sum(1 for r in recs if r.get("abstain"))
    prod_unknown = sum(1 for r in recs if str(r.get("product")) in ("未知", "待确认", "None", ""))
    dir_unknown = sum(1 for r in recs if str(r.get("direction")) in ("未知", "待确认", "None", ""))
    print(f"  弃权(abstain)       : {unclassified}  ({unclassified / total * 100:.1f}%)")
    print(f"  产品未知            : {prod_unknown}  ({prod_unknown / total * 100:.1f}%)")
    print(f"  方向未知            : {dir_unknown}  ({dir_unknown / total * 100:.1f}%)")
    cached = sum(1 for r in recs if r.get("cached"))
    print(f"  命中本地缓存        : {cached}  ({cached / total * 100:.1f}%)")
    conf = [float(r.get("confidence") or 0) for r in recs]
    conf.sort()
    if conf:
        print(f"  置信度分位          : p10={conf[int(total*0.1)]:.2f} "
              f"p50={conf[total//2]:.2f} p90={conf[int(total*0.9)]:.2f}")

    # ------------------------------------------------------ 二、门控归因
    section("二、门控归因（源方向有值、却被门控成未知）")
    gated: Counter[tuple[str, str, str]] = Counter()
    for r in recs:
        m = GATE_RE.search(str(r.get("reason") or ""))
        if not m:
            continue
        src_product, src_direction, _grp, _prod, final_direction = (
            m.group(1).strip(), m.group(2).strip(), m.group(3), m.group(4), m.group(5).strip())
        if final_direction == "未知" and src_direction not in ("未知", "待确认", "未知方向"):
            gated[(src_product, src_direction, str(r.get("product")))] += 1
    if gated:
        print(f"  共 {sum(gated.values())} 条被门控掉（这是最值得修的一类：已识别的信息被规则自己擦掉）")
        for (sp, sd, fp), n in gated.most_common(args.top):
            print(f"    {n:>4}  {sp}/{sd}  →  产品落成 {fp}")
    else:
        print("  ✓ 没有方向被门控成未知")

    # ------------------------------------------------------ 三、零命中词
    section("三、证据词命中统计（含 0 命中的疑似死词表）")
    tables: list[tuple[str, dict]] = []
    for name in ("PRODUCT_RULES", "LEGACY_PRODUCT_RULES"):
        for target, groups in getattr(cpa, name, {}).items():
            tables.append((f"{name}/{target}", groups))
    for target, rules in cpa.DIRECTION_RULES.items():
        tables.append((f"DIRECTION_RULES/{target}", {"positive": rules}))
    for target, rules in getattr(cpa, "LEGACY_DIRECTION_RULES", {}).items():
        tables.append((f"LEGACY_DIRECTION_RULES/{target}", {"positive": rules}))
    for line, rules in cpa.BUSINESS_LINE_RULES.items():
        tables.append((f"BUSINESS_LINE_RULES/{line}", {"positive": rules}))

    joined = list(corpora.values())
    appeared_products = {str(r.get("product")) for r in recs}
    appeared_directions = {str(r.get("direction")) for r in recs}
    zero: list[tuple[str, str]] = []
    zero_relevant: list[tuple[str, str]] = []
    total_patterns = 0
    for label, groups in tables:
        for pattern, _weight in groups.get("positive", []):
            total_patterns += 1
            token = cpa.compact_text(str(pattern))
            if not token:
                continue
            if any(token in c for c in joined):
                continue
            zero.append((label, str(pattern)))
            # 目标本批确实产出了结果，但这条证据词一次都没命中 → 更可疑
            target = label.split("/", 1)[-1]
            if target in appeared_products or target in appeared_directions:
                zero_relevant.append((label, str(pattern)))
    print(f"  共 {total_patterns} 条证据词，本批 {len(zero)} 条零命中；"
          f"其中 {len(zero_relevant)} 条更可疑（目标本批有结果，但这词一次没命中）")
    print("  ⚠ 零命中不等于死词表：本批素材没用到也会零命中，判死要跨批次累计。")
    print("    今天那个真死词表（找「大号标题/主标题/标题区」，模型只会写「大号文字主导」）")
    print("    是写在代码谓词里的，不在这些表里，本脚本覆盖不到 —— 那类要靠 check_rules 之外的人工复核。")
    if zero_relevant:
        print("\n  【优先看】目标本批有结果、但证据词零命中：")
        for label, pattern in zero_relevant[:args.top]:
            print(f"    0  {label}  「{pattern}」")
        if len(zero_relevant) > args.top:
            print(f"    …… 另有 {len(zero_relevant) - args.top} 条")
    other = [z for z in zero if z not in set(zero_relevant)]
    if other:
        print(f"\n  其他零命中（目标本批也没有结果，多半本批用不上）：{len(other)} 条")
        for label, pattern in other[:max(0, args.top // 2)]:
            print(f"    0  {label}  「{pattern}」")

    # -------------------------------------------------- 四、新方向候选
    section("四、新方向候选（模型给出、但不在任何产品允许集里）")
    allowed_all = {
        d
        for spec in cpa.COLLECTION_TAXONOMY.get("products", {}).values()
        for d in spec.get("directions", [])
    } | set(cpa.COLLECTION_DERIVED_DIRECTIONS) | {"未知"}
    novel: Counter[str] = Counter()
    for r in recs:
        for field in ("detected_direction", "direction"):
            value = str(r.get(field) or "").strip()
            if value and value not in allowed_all:
                novel[value] += 1
        debug = r.get("rule_debug") or {}
        for cand in (debug.get("direction_candidates") or [])[:3]:
            name = str(cand.get("name") or "").strip()
            if name and name not in allowed_all and (cand.get("score") or 0) > 0:
                novel[name] += 1
    if novel:
        print("  出现次数（达到阈值的可以考虑晋升进分类表）：")
        for name, n in novel.most_common(args.top):
            print(f"    {n:>4}  {name}")
    else:
        print("  ✓ 没有表外方向")

    # ------------------------------------------------------ 五、分布
    section("五、分类结果分布")
    for field in ("product", "direction"):
        print(f"  {field}:")
        for k, v in Counter(str(r.get(field)) for r in recs).most_common(args.top):
            print(f"    {v:>5}  {v / total * 100:5.1f}%  {k}")

    # ------------------------------------------- 六、与文件名历史标签的对比
    if args.source:
        section("六、与文件名历史标签的对比（文件名只是弱标签，仅供参照）")
        src_dir = Path(args.source)
        if not src_dir.is_dir():
            print(f"  ! 目录不存在：{src_dir}")
        else:
            products = sorted(cpa.PRODUCT_DIRECTIONS, key=len, reverse=True)
            directions = sorted(
                {d for ds in cpa.PRODUCT_DIRECTIONS.values() for d in ds}, key=len, reverse=True)
            noise = {"纯ai", "ai策略", "aigc", "大图", "闪屏", "大盘跑量", "跑量", "跑量衍生", "衍生", "权益"}

            def label_of(name: str):
                stem = re.sub(r"\.(jpg|jpeg|png|webp)$", "", name, flags=re.I)
                toks = [t.strip() for t in stem.split("-") if t.strip()]
                p = d = None
                idx = None
                for i, t in enumerate(toks):
                    for cand in products:
                        if t == cand or t.startswith(cand):
                            p, idx = cand, i
                            break
                    if p:
                        break
                if idx is not None:
                    for t in toks[idx + 1:]:
                        if t.lower() in noise:
                            continue
                        for cand in directions:
                            if t == cand or t.startswith(cand):
                                d = cand
                                break
                        if d:
                            break
                return p, d

            pk = pb = dk = db = dn = 0
            for r in recs:
                lp, ld = label_of(str(r.get("file", "")))
                if lp:
                    (pk := pk + 1) if str(r.get("product")) == lp else (pb := pb + 1)
                if ld:
                    got = str(r.get("direction"))
                    if got == ld:
                        dk += 1
                    elif got in ("未知", "待确认", ""):
                        dn += 1
                    else:
                        db += 1
            if pk + pb:
                print(f"  产品一致 {pk}/{pk + pb} = {pk / (pk + pb) * 100:.1f}%")
            if dk + db + dn:
                print(f"  方向一致 {dk}/{dk + db + dn} = {dk / (dk + db + dn) * 100:.1f}%"
                      f"（其中判成未知 {dn}、判错 {db}）")
            print("  ⚠ 文件名是人工整理的弱标签，今天的实测里它自己错过（百万防癌险被标成百万医疗险），")
            print("    所以这里的不一致只是线索，判谁对要看图。")

    print()


if __name__ == "__main__":
    main()
