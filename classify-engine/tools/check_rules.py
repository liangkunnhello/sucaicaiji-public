"""规则一致性校验器：只静态检查规则本身是否自洽，不跑分类、不调 API。

存在的意义：今天（2026-09-21）连着踩的三类坑，本来都该被这个脚本自动发现——

1. 内部词表有「上下左右图文」、对外分类表没有
   → 110 张图被分类表门控成「未知」（规则自己把分类擦掉了）
2. 业务线词表只认「快手app」、不认「快手」/「快手搜索」
   → 233 张业务线得分全为 0，整批判不出来
3. 加一个产品要同步七处表，漏一处就 KeyError

用法：
    python tools/check_rules.py            # 人读的报告
    python tools/check_rules.py --quiet    # 只输出问题，退出码非 0 表示有 ERROR（给 CI 用）
    python tools/check_rules.py --json     # 机器可读
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import classify_pure_api as cpa  # noqa: E402

TAG = {"ERROR": "✗", "WARN": "!", "INFO": "·"}
issues: list[tuple[str, str, str]] = []          # (level, section, message)


def add(level: str, section: str, message: str) -> None:
    issues.append((level, section, message))


# ---------------------------------------------------------------- C1 分类表结构
def check_taxonomy_shape() -> None:
    section = "C1 分类表结构"
    products = cpa.COLLECTION_TAXONOMY.get("products", {})
    if not products:
        add("ERROR", section, "分类表里一个产品都没有")
        return
    unknown = str(cpa.COLLECTION_TAXONOMY.get("unknown_direction", ""))
    derived = str(cpa.COLLECTION_TAXONOMY.get("derived_direction", ""))
    for name, spec in products.items():
        directions = list(spec.get("directions", []))
        if unknown and unknown not in directions:
            add("ERROR", section, f"{name}: 缺少未知方向「{unknown}」，会无处可落")
        if derived and derived not in directions:
            add("ERROR", section, f"{name}: 缺少衍生方向「{derived}」")
        dup = {d for d in directions if directions.count(d) > 1}
        if dup:
            add("WARN", section, f"{name}: 方向重复 {sorted(dup)}")
        if not spec.get("group"):
            add("ERROR", section, f"{name}: 没有所属一级分类")
    add("INFO", section, f"共 {len(products)} 个产品")


def normalize_direction(direction: str) -> str:
    """把方向归一成分类表口径，避免把「同一个概念的两种写法」当成不一致。

    三处归一必须都做，否则全是误报：
    1. DIRECTION_ALIASES        内部别名（左右图文 → 上下左右图文）
    2. COLLECTION_DIRECTION_ALIASES 分类表别名（未细分 → 未知）
    3. COLLECTION_DERIVED_DIRECTIONS → derived_direction
       （自动衍生/妙思自动衍生/秒思自动衍生/妙思衍生 是同一个东西；
        内部词表在导入时会自动给每个产品追加「秒思自动衍生」，不归一就会报 14 次假 ERROR）
    """
    name = cpa.COLLECTION_DIRECTION_ALIASES.get(
        cpa.canonical_direction_name(direction),
        cpa.canonical_direction_name(direction),
    )
    if name in cpa.COLLECTION_DERIVED_DIRECTIONS:
        return str(cpa.COLLECTION_TAXONOMY.get("derived_direction", name))
    return name


# ------------------------------------------------- C2 内部词表 vs 对外分类表
def check_vocabulary_gap(verbose: bool = False) -> None:
    """今天最大的坑：内部能产出的方向，分类表里没有 → 被门控成未知。"""
    section = "C2 内部词表/分类表 方向差集"
    taxonomy_products = cpa.COLLECTION_TAXONOMY.get("products", {})
    unknown = str(cpa.COLLECTION_TAXONOMY.get("unknown_direction", "未知"))
    gated_total = 0
    orphan_total = 0
    orphans: list[str] = []

    for product, internal in cpa.PRODUCT_DIRECTIONS.items():
        target = cpa.COLLECTION_PRODUCT_ALIASES.get(product, product)
        spec = taxonomy_products.get(target)
        if spec is None:
            add("WARN", section,
                f"{product}: 不在对外分类表里，它产出的结果会整体落「未知/未知」")
            continue
        allowed = {normalize_direction(d) for d in spec.get("directions", [])}
        internal_canonical = {normalize_direction(d) for d in set(internal)}

        # 内部能产出、但分类表不允许 → 会被门控成未知（严重，会静默丢掉已识别的方向）
        gated = sorted(internal_canonical - allowed - {unknown})
        if gated:
            gated_total += len(gated)
            add("ERROR", section,
                f"{product}: 内部可产出但分类表不允许 {gated} —— 命中就会被门控成「{unknown}」")

        # 分类表声明、但内部词表产不出 → 死方向（不严重，但说明表在骗人）
        orphan = sorted(allowed - internal_canonical)
        if orphan:
            orphan_total += len(orphan)
            orphans.append(f"{product}({len(orphan)}: {'/'.join(orphan)})")

    if orphans:
        if verbose:
            for line in orphans:
                add("WARN", section, f"分类表声明了、但内部词表产不出 {line}")
        else:
            add("WARN", section,
                f"{len(orphans)} 个产品声明了内部词表产不出的方向，共 {orphan_total} 个"
                f"（加 --verbose 看明细）")
    if not gated_total:
        add("INFO", section, "没有方向会被门控成未知")
    add("INFO", section, f"会被门控的方向 {gated_total} 个，产不出的方向 {orphan_total} 个")


# --------------------------------------------------- C3 七处表的产品 key 同步
# 加一个产品要动的位置；漏任何一处都会 KeyError 或产品静默识别不出来
def check_table_sync() -> None:
    section = "C3 产品表同步"
    tables = [
        ("PRODUCT_DIRECTIONS", set(cpa.PRODUCT_DIRECTIONS)),
        ("LEGACY_PRODUCT_DIRECTIONS", set(cpa.LEGACY_PRODUCT_DIRECTIONS)),
        ("PRODUCT_RULES", set(cpa.PRODUCT_RULES)),
        ("LEGACY_PRODUCT_RULES", set(cpa.LEGACY_PRODUCT_RULES)),
        ("PRODUCT_BUSINESS_LINES", set(cpa.PRODUCT_BUSINESS_LINES)),
        ("分类表 products", set(cpa.COLLECTION_TAXONOMY.get("products", {}))),
    ]
    # 分类表用别名后的名字，比较前先统一
    alias_to_raw = {v: k for k, v in cpa.COLLECTION_PRODUCT_ALIASES.items()}
    normalized = {}
    for name, keys in tables:
        normalized[name] = {alias_to_raw.get(k, k) for k in keys}

    universe = set().union(*normalized.values())
    for name, keys in normalized.items():
        missing = sorted(universe - keys)
        if missing:
            add("ERROR", section, f"{name} 缺少 {missing}")
        extra = sorted(keys - normalized["PRODUCT_DIRECTIONS"])
        if extra:
            add("WARN", section, f"{name} 多出（内部词表没有）{extra}")
    names = [n for n, _ in tables]
    add("INFO", section, f"共 {len(universe)} 个产品，已比对 {len(names)} 张表")


# ------------------------------------------------------------ C4 别名有效性
def check_aliases() -> None:
    section = "C4 别名有效性"
    all_internal = {cpa.canonical_direction_name(d) for ds in cpa.PRODUCT_DIRECTIONS.values() for d in ds}
    all_taxonomy = {
        d for spec in cpa.COLLECTION_TAXONOMY.get("products", {}).values()
        for d in spec.get("directions", [])
    }
    for src, dst in cpa.DIRECTION_ALIASES.items():
        if cpa.canonical_direction_name(dst) not in all_internal:
            add("WARN", section, f"DIRECTION_ALIASES[{src}] → {dst}，但目标不在任何产品的内部词表里")
    for src, dst in cpa.COLLECTION_DIRECTION_ALIASES.items():
        if dst not in all_taxonomy:
            add("WARN", section, f"COLLECTION_DIRECTION_ALIASES[{src}] → {dst}，但目标不在分类表任何产品里")
    add("INFO", section,
        f"内部别名 {len(cpa.DIRECTION_ALIASES)} 条，分类表别名 {len(cpa.COLLECTION_DIRECTION_ALIASES)} 条")


# ------------------------------------------------------- C5 证据词表基本健康
def check_evidence_tables() -> None:
    section = "C5 证据词表"
    empty = 0
    dup = 0
    for table_name, table in (
        ("PRODUCT_RULES", cpa.PRODUCT_RULES),
        ("BUSINESS_LINE_RULES", cpa.BUSINESS_LINE_RULES),
    ):
        for target, groups in table.items():
            positives = groups.get("positive", []) if isinstance(groups, dict) else groups
            seen: dict[str, int] = {}
            for pattern, weight in positives:
                if not str(pattern).strip():
                    empty += 1
                    add("ERROR", section, f"{table_name}/{target}: 有空证据词")
                key = str(pattern)
                if key in seen and seen[key] != weight:
                    dup += 1
                    add("WARN", section, f"{table_name}/{target}: 「{key}」权重不一致 {seen[key]} vs {weight}")
                seen[key] = weight
    for target, rules in cpa.DIRECTION_RULES.items():
        seen = {}
        for pattern, weight in rules:
            if not str(pattern).strip():
                empty += 1
                add("ERROR", section, f"DIRECTION_RULES/{target}: 有空证据词")
            key = str(pattern)
            if key in seen and seen[key] != weight:
                dup += 1
                add("WARN", section, f"DIRECTION_RULES/{target}: 「{key}」权重不一致 {seen[key]} vs {weight}")
            seen[key] = weight
    add("INFO", section, f"空词 {empty} 个，同表内权重冲突 {dup} 处")


# ---------------------------------------------------------------- C6 方向判据覆盖
# 少数方向的判据写在**代码谓词**里而不是证据表里。登记在这里，工具才知道它们
# 「有判据」，不会误报。这类判据是一处技术债：它不受 C5（证据词体检）保护。
DIRECTIONS_WITH_CODE_PREDICATE: dict[str, str] = {
    "海报": "has_text_led_poster_evidence()",
}


def check_direction_coverage() -> None:
    """每个方向都要有能判出它的路径，否则永远落「未知」。

    2026-09-23 体检发现：45 个方向里 12 个在 DIRECTION_RULES、
    LEGACY_DIRECTION_RULES、别名链、代码谓词里**全找不到判据** ——
    策略中心建了方向、本地没人写规则，命中的素材 100% 落未知。
    与 `has_clear_poster_evidence` 死词表同源：建了概念、忘了实现。
    """
    section = "C6 方向判据覆盖"
    products = cpa.COLLECTION_TAXONOMY.get("products", {})
    unknown = str(cpa.COLLECTION_TAXONOMY.get("unknown_direction", ""))
    derived = str(cpa.COLLECTION_TAXONOMY.get("derived_direction", ""))

    direct = set(cpa.DIRECTION_RULES) | set(cpa.LEGACY_DIRECTION_RULES)
    alias_sources: dict[str, list[str]] = {}
    for src, dst in cpa.COLLECTION_DIRECTION_ALIASES.items():
        alias_sources.setdefault(str(dst), []).append(str(src))

    coverage: dict[str, list[str]] = {}
    for spec in products.values():
        for d in spec.get("directions", []):
            if d in coverage or d in (unknown, derived):
                continue
            sources: list[str] = []
            if d in direct:
                sources.append("证据表")
            for src in alias_sources.get(d, []):
                if src in direct:
                    sources.append(f"别名←{src}")
            if d in cpa.COLLECTION_DERIVED_DIRECTIONS:
                sources.append("派生归一")
            if d in DIRECTIONS_WITH_CODE_PREDICATE:
                sources.append(DIRECTIONS_WITH_CODE_PREDICATE[d])
            coverage[d] = sources

    dead = sorted(d for d, srcs in coverage.items() if not srcs)
    if dead:
        add("ERROR", section,
            f"{len(dead)} 个方向没有任何判据，命中的素材会 100% 落「{unknown}」：")
        for d in dead:
            owners = [p for p, s in products.items() if d in s.get("directions", [])]
            add("ERROR", section, f"  {d}（属于 {', '.join(owners)}）")
        add("INFO", section,
            "处置二选一：补证据表（需要业务描述这些方向长什么样），"
            "或从 taxonomy_overrides.json / 策略中心里去掉该方向。")
    else:
        add("INFO", section, "每个方向都有可判出的路径")
    add("INFO", section, f"共 {len(coverage)} 个方向，无判据 {len(dead)} 个")


# ---------------------------------------------------------------- C7 违规拦截规则
def check_violation_rules() -> None:
    """违规拦截只在命中时才体现，配错了完全静默 —— 必须静态校验。

    最容易错的是 match_fields 拼错字段名：规则看着正常，但永远拿不到语料、
    永远不命中，且不报任何错。这类「静默失效」正是本项目反复吃亏的地方。
    """
    section = "C7 违规拦截规则"
    rules = cpa.load_violation_rules().get("rules") or []
    known_fields = (
        set(cpa.KEYWORD_PROMPT_FIELDS)
        | set(cpa.STRUCTURED_PROMPT_FIELDS)
        | set(cpa.OPTIONAL_STRUCTURED_PROMPT_FIELDS)
    )
    if not rules:
        add("WARN", section, "没有任何违规拦截规则")
    for rule in rules:
        name = str(rule.get("name") or rule.get("id") or "?")
        if not rule.get("enabled", True):
            add("INFO", section, f"{name}: 已停用")
            continue
        if not rule.get("keywords"):
            add("ERROR", section, f"{name}: 没有关键词，永远不会命中")
        direction = str(rule.get("direction") or "").strip()
        if not direction:
            add("ERROR", section, f"{name}: 没有落点方向")
        lines = rule.get("business_lines") or []
        add("INFO", section,
            f"{name}: 落「{direction}」· 业务线 {lines or '全部'} · "
            f"关键词 {len(rule.get('keywords') or [])} 个")
        fields = [str(f) for f in (rule.get("match_fields") or [])]
        for field in fields:
            if field not in known_fields:
                add("ERROR", section,
                    f"{name}: match_fields 引用了不存在的字段「{field}」"
                    f" —— 该规则会静默失效（永远拿不到语料）")
            elif field not in cpa.VIOLATION_FIELD_NAMES:
                # 字段本身存在，但没登记进按字段切语料的白名单 ——
                # violation_field_corpora() 不会为它生成语料，规则同样永远不命中。
                add("ERROR", section,
                    f"{name}: match_fields 的「{field}」没有登记进 "
                    f"VIOLATION_FIELD_NAMES —— 该规则会静默失效（拿不到字段语料）")
        if fields:
            add("INFO", section, f"{name}: 只在字段 {fields} 里匹配（不扫全语料）")
            if rule.get("include_uncertain"):
                add("WARN", section,
                    f"{name}: 同时声明了 match_fields 与 include_uncertain；"
                    f"限定字段时不参与 uncertain 匹配，该开关不会生效")
    add("INFO", section, f"共 {len(rules)} 条规则")


# ---------------------------------------------------------------------- 主流程
def main() -> None:
    parser = argparse.ArgumentParser(description="规则一致性校验器")
    parser.add_argument("--quiet", action="store_true", help="只输出 ERROR/WARN，不打 INFO")
    parser.add_argument("--verbose", action="store_true", help="展开明细（如每个产品的死方向清单）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    check_taxonomy_shape()
    check_vocabulary_gap(verbose=args.verbose)
    check_table_sync()
    check_aliases()
    check_evidence_tables()
    check_direction_coverage()
    check_violation_rules()

    errors = [i for i in issues if i[0] == "ERROR"]
    warns = [i for i in issues if i[0] == "WARN"]

    if args.json:
        print(json.dumps(
            [{"level": lv, "section": s, "message": m} for lv, s, m in issues],
            ensure_ascii=False, indent=2))
        return

    current = ""
    for level, section, message in issues:
        if section != current:
            current = section
            print(f"\n{'=' * 70}\n{section}\n{'=' * 70}")
        if args.quiet and level == "INFO":
            continue
        print(f"  {TAG[level]} {message}")

    print(f"\n{'=' * 70}")
    print(f"ERROR {len(errors)} 项 · WARN {len(warns)} 项")
    if errors:
        print("✗ 有 ERROR：这些会直接造成错分或丢分，建议修掉再跑批次")
    elif warns:
        print("! 无 ERROR，但有 WARN，值得看一眼")
    else:
        print("✓ 规则自洽")
    print("=" * 70)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
