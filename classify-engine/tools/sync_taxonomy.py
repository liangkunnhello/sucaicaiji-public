#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把策略中心（业务目录服务）的业务目录同步成本地分类表。

策略中心是「业务线-产品-方向」的唯一权威来源；
本工具拉取它的分组/产品/方向，叠加 taxonomy_overrides.json 里登记的例外，
生成 classify-engine/scripts/collection_taxonomy_v1.json。

用法：
    python tools/sync_taxonomy.py              # 只对比，不改文件；有漂移退出码 1
    python tools/sync_taxonomy.py --apply      # 写回分类表
    python tools/sync_taxonomy.py --dump-raw   # 只打印远端原始数据

为什么要有这个工具：
    分类表以前靠人手维护，和「内部词表 / 七处产品表」一起靠人脑保持一致 ——
    结果是方向被门控成「未知」、产品整条落「未知/未知」这类静默丢分，
    查半天才发现是某张表漏了一个词。让分类表自动生成，这类不一致就不会再有。

依赖：requests（内置运行时与系统 Python 都有）。不依赖 mcp 包。

环境变量（三项都必填，无内置默认值）：
    LINGJING_BASE      业务目录服务的后端地址，如 https://your-host.example.com
    LINGJING_USER      登录账号
    LINGJING_PASSWORD  登录密码
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from datetime import date
from pathlib import Path

import requests

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                                  line_buffering=True)

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
TAXONOMY_PATH = SCRIPTS / "collection_taxonomy_v1.json"
OVERRIDES_PATH = SCRIPTS / "taxonomy_overrides.json"

BASE = os.environ.get("LINGJING_BASE", "").rstrip("/")
USER = os.environ.get("LINGJING_USER", "")
PASSWORD = os.environ.get("LINGJING_PASSWORD", "")


def _require_env() -> None:
    """三项配置缺一不可，缺失时直接报错，避免拿空账号去撞接口。"""
    missing = [
        name for name, value in (
            ("LINGJING_BASE", BASE),
            ("LINGJING_USER", USER),
            ("LINGJING_PASSWORD", PASSWORD),
        ) if not value
    ]
    if missing:
        raise SystemExit(
            "缺少环境变量：" + ", ".join(missing)
            + "\n请先设置业务目录服务的地址与账号后再运行。"
        )
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class SyncError(RuntimeError):
    pass


# --------------------------------------------------------------- 策略中心接口

class Lingjing:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "X-Requested-With": "XMLHttpRequest"})

    def login(self) -> None:
        resp = self.session.post(
            f"{BASE}/login",
            data={"username": USER, "password": PASSWORD, "rememberMe": "false"},
            timeout=30,
        )
        try:
            body = resp.json()
        except ValueError as error:
            raise SyncError(f"登录响应非 JSON（HTTP {resp.status_code}）") from error
        if body.get("code") != 0:
            raise SyncError(f"登录失败：{body.get('msg')}")
        print(f"✓ 已登录策略中心：{USER}")

    def get(self, path: str):
        resp = self.session.get(BASE + path, timeout=60)
        try:
            body = resp.json()
        except ValueError as error:
            raise SyncError(f"{path} 响应非 JSON（HTTP {resp.status_code}）") from error
        if body.get("code") != 0:
            raise SyncError(f"{path} 返回失败：{body.get('msg')}")
        return body.get("data")


def fetch_raw(dump_only: bool = False) -> dict:
    api = Lingjing()
    api.login()
    groups = api.get("/aiimg/group/all") or []
    products = api.get("/aiimg/product/all") or []
    directions = api.get("/aiimg/direction/all") or []
    counts = api.get("/aiimg/strategy/counts-by-direction") or []
    print(f"✓ 策略中心目录：{len(groups)} 业务线 / {len(products)} 产品 / {len(directions)} 方向")
    if dump_only:
        print(json.dumps({"groups": groups, "products": products,
                          "directions": directions, "counts": counts},
                         ensure_ascii=False, indent=2))
    return {"groups": groups, "products": products,
            "directions": directions, "counts": counts}


# --------------------------------------------------------------- 生成分类表

def load_overrides() -> dict:
    try:
        return json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"⚠ 未找到 {OVERRIDES_PATH.name}，按「策略中心全量、无例外」处理")
        return {}
    except ValueError as error:
        raise SyncError(f"{OVERRIDES_PATH.name} 不是合法 JSON：{error}") from error


def build_taxonomy(raw: dict, overrides: dict) -> tuple[dict, list[str]]:
    groups = sorted(raw["groups"], key=lambda g: g.get("id") or 0)
    products = sorted(raw["products"], key=lambda p: p.get("id") or 0)
    directions = sorted(raw["directions"], key=lambda d: d.get("id") or 0)

    tech = list(overrides.get("technical_directions") or ["未知", "妙思衍生"])
    extra_products = overrides.get("extra_products") or {}
    extra_directions = overrides.get("extra_directions") or {}

    by_product: dict[int, list[dict]] = {}
    for direction in directions:
        by_product.setdefault(direction.get("productId"), []).append(direction)

    notes: list[str] = []
    products_out: dict[str, dict] = {}

    for group in groups:
        group_name = group.get("groupName")
        for product in products:
            if product.get("groupId") != group.get("id"):
                continue
            name = product.get("productName")
            dirs: list[str] = []
            for direction in by_product.get(product.get("id"), []):
                dname = direction.get("directionName")
                if dname and dname not in dirs:
                    dirs.append(dname)
            for extra in extra_directions.get(name, []):
                if extra not in dirs:
                    dirs.append(extra)
                    notes.append(f"{name}: 补登记方向「{extra}」（策略中心没有，本地点已用）")
            for t in tech:
                if t not in dirs:
                    dirs.append(t)
            products_out[name] = {"group": group_name, "directions": dirs}

    for name, spec in extra_products.items():
        if name in products_out:
            notes.append(f"⚠ {name} 策略中心已有，忽略 extra_products 里的登记")
            continue
        dirs = list(spec.get("directions") or [])
        # 例外产品同样吃 extra_directions —— 否则很容易踩坑：往 extra_directions 里
        # 给一个「策略中心没有的产品」登记方向会被静默忽略（它不在策略中心的循环里）。
        for extra in extra_directions.get(name, []):
            if extra not in dirs:
                dirs.append(extra)
        for t in tech:
            if t not in dirs:
                dirs.append(t)
        products_out[name] = {"group": spec.get("group") or "未知", "directions": dirs}
        notes.append(f"{name}: 补登记产品（业务线 {spec.get('group')}）（策略中心没有，本地点已用）")

    stamp = date.today().strftime("%Y%m%d")
    taxonomy = {
        "version": (f"collection-taxonomy-{stamp}-lingjing-"
                    f"{len(groups)}g{len(products)}p{len(directions)}d"),
        "source": {
            "system": "策略中心",
            "base": BASE,
            "synced_at": date.today().isoformat(),
            "groups": len(groups),
            "products": len(products),
            "directions": len(directions),
            "strategy_total": sum(int(c.get("strategyCount") or 0) for c in raw["counts"]),
            "generated_by": "tools/sync_taxonomy.py",
        },
        "unknown_direction": tech[0] if tech else "未知",
        "derived_direction": tech[1] if len(tech) > 1 else "妙思衍生",
        "products": products_out,
    }
    return taxonomy, notes


# --------------------------------------------------------------- 对比与写回

def diff_taxonomy(old: dict, new: dict) -> list[str]:
    lines: list[str] = []
    op = old.get("products", {})
    np_ = new.get("products", {})
    added = sorted(set(np_) - set(op))
    removed = sorted(set(op) - set(np_))
    if added:
        lines.append(f"新增产品 {len(added)}：{added}")
    if removed:
        lines.append(f"移除产品 {len(removed)}：{removed}")
    for name in sorted(set(op) & set(np_)):
        old_spec, new_spec = op[name], np_[name]
        if old_spec.get("group") != new_spec.get("group"):
            lines.append(f"{name}: 业务线 {old_spec.get('group')} → {new_spec.get('group')}")
        od = list(old_spec.get("directions") or [])
        nd = list(new_spec.get("directions") or [])
        plus = [d for d in nd if d not in od]
        minus = [d for d in od if d not in nd]
        if plus or minus:
            detail = []
            if plus:
                detail.append(f"+{plus}")
            if minus:
                detail.append(f"-{minus}")
            lines.append(f"{name}: {' '.join(detail)}")
    if not lines:
        lines.append("(无差异)")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="把策略中心的业务目录同步成本地分类表")
    parser.add_argument("--apply", action="store_true", help="写回 collection_taxonomy_v1.json")
    parser.add_argument("--dump-raw", action="store_true", help="只打印策略中心原始数据")
    args = parser.parse_args()

    raw = fetch_raw(dump_only=args.dump_raw)
    if args.dump_raw:
        return 0

    overrides = load_overrides()
    new_taxonomy, notes = build_taxonomy(raw, overrides)

    old_taxonomy: dict = {}
    if TAXONOMY_PATH.exists():
        try:
            old_taxonomy = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
        except ValueError:
            old_taxonomy = {}

    print()
    print("=" * 74)
    print(f"当前分类表：{old_taxonomy.get('version', '(无)')}  "
          f"{len(old_taxonomy.get('products') or {})} 产品")
    print(f"同步后分类表：{new_taxonomy['version']}  {len(new_taxonomy['products'])} 产品")
    print("=" * 74)
    print("差异：")
    for line in diff_taxonomy(old_taxonomy, new_taxonomy):
        print("   ", line)
    if notes:
        print("\n例外登记：")
        for note in notes:
            print("   ", note)

    if not args.apply:
        drifted = diff_taxonomy(old_taxonomy, new_taxonomy)[0] != "(无差异)"
        print()
        print("（预览模式，未改动文件。加 --apply 写回）")
        return 1 if drifted else 0

    TAXONOMY_PATH.write_text(
        json.dumps(new_taxonomy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n✓ 已写回 {TAXONOMY_PATH}")
    print("  下一步：跑 tools/check_rules.py 确认内部词表与分类表没有新的方向差集")
    return 0


if __name__ == "__main__":
    try:
        _require_env()
        raise SystemExit(main())
    except SyncError as error:
        print(f"✗ {error}")
        raise SystemExit(2)
