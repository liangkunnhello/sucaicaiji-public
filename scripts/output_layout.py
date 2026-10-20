"""素材下载器统一输出目录。

所有平台统一使用：
  导出根目录 / 平台 / YYYYMMDD / 原始素材
                              / 报表
                              / 分类结果
"""

from __future__ import annotations

import os
import re
import json
from datetime import datetime
from pathlib import Path


DEFAULT_EXPORT_ROOT = Path(r"D:\跑量素材\外部跑量素材")


def _batch_date() -> str:
    value = os.environ.get("MATERIAL_DOWNLOADER_BATCH_DATE", "").strip()
    if re.fullmatch(r"\d{8}", value):
        return value
    return datetime.now().strftime("%Y%m%d")


def _next_batch_dir(root: Path, platform_name: str, batch_date: str) -> tuple[str, Path]:
    """当天同一平台只用一个日期目录，多次采集（不同方向）并进来。

    以前会依次生成 -2、-3 后缀目录，导致同一天的素材散落在多个目录里。
    合并是安全的：下载脚本按文件名跳过已存在的文件，分类审计是追加式且带断点续跑。
    """
    platform_dir = root / platform_name
    return batch_date, platform_dir / batch_date


def _media_subdir(platform_name: str) -> str:
    """内部系统在日期下再分「图片 / 视频」两层；其他平台不分。

    图片走「下载 + 分类」，视频只下载不分类，两者产物结构不同
    （视频没有分类结果），混在一个目录里没法用。
    素材类型由桌面端通过 MATERIAL_DOWNLOADER_INTERNAL_MATERIAL_TYPE 传入。
    """
    if platform_name != "内部系统":
        return ""
    media_type = os.environ.get("MATERIAL_DOWNLOADER_INTERNAL_MATERIAL_TYPE", "").strip()
    return media_type if media_type in ("图片", "视频") else ""


def get_batch_layout(
    platform_name: str,
    export_root: str | os.PathLike[str] | None = None,
    *,
    create: bool = True,
) -> dict[str, Path]:
    """返回某个平台本次下载批次的统一目录。"""
    root = Path(
        export_root
        or os.environ.get("MATERIAL_DOWNLOADER_EXPORT_DIR")
        or DEFAULT_EXPORT_ROOT
    ).expanduser()
    batch_date = _batch_date()
    media_subdir = _media_subdir(platform_name)
    configured_batch_dir = os.environ.get("MATERIAL_DOWNLOADER_BATCH_DIR")
    if configured_batch_dir:
        batch_dir = Path(configured_batch_dir)
        batch_name = batch_dir.name
    else:
        batch_name, date_dir = _next_batch_dir(root, platform_name, batch_date)
        batch_dir = date_dir / media_subdir if media_subdir else date_dir
    raw_dir = Path(
        os.environ.get("MATERIAL_DOWNLOADER_RAW_DIR")
        or batch_dir / "原始素材"
    )
    report_dir = Path(
        os.environ.get("MATERIAL_DOWNLOADER_REPORT_DIR")
        or batch_dir / "报表"
    )
    classification_dir = Path(
        os.environ.get("MATERIAL_DOWNLOADER_CLASSIFICATION_DIR")
        or batch_dir / "分类结果"
    )

    if create:
        directories = [raw_dir, report_dir]
        # 视频只下载不分类，不建分类结果目录，免得留个空壳让人误以为该有分类产物
        if media_subdir != "视频":
            directories.append(classification_dir)
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)

    return {
        "root": root,
        "batch_date": batch_date,
        "batch_name": batch_name,
        # batch_dir = 本次运行的产出根；下游按 batch_dir/分类结果 取件仍然成立
        "batch_dir": batch_dir,
        "media_dir": batch_dir,
        "media_type": media_subdir,
        "date_dir": batch_dir.parent if media_subdir else batch_dir,
        "raw_dir": raw_dir,
        "report_dir": report_dir,
        "classification_dir": classification_dir,
    }


def resolve_batch_paths(batch_dir: Path) -> dict[str, Path | None]:
    """把一个批次目录解析成各子目录，**兼容新旧两种结构**。

    旧结构（所有平台的历史批次、其他平台的现在）：
        日期/原始素材、日期/报表、日期/分类结果
    新结构（内部系统，2026-09-28 起）：
        日期/图片/原始素材、日期/图片/分类结果、日期/图片/报表
        日期/视频/原始素材、日期/视频/报表（视频只下载不分类，没有分类结果）

    工具（重跑、对比、校验）都该用这个函数定位，别写死「日期/分类结果」。
    """
    batch_dir = Path(batch_dir)
    candidates = [batch_dir]
    for media in ("图片", "视频"):
        media_dir = batch_dir / media
        if media_dir.is_dir():
            candidates.append(media_dir)

    for base in candidates:
        classification = base / "分类结果"
        if (classification / "_API分类审计.jsonl").is_file():
            return {
                "base": base,
                "media_type": base.name if base != batch_dir else "",
                "raw_dir": base / "原始素材",
                "report_dir": base / "报表",
                "classification_dir": classification,
            }

    # 没有审计也返回结构，方便调用方报错时给出准确路径
    base = batch_dir
    return {
        "base": base,
        "media_type": "",
        "raw_dir": base / "原始素材",
        "report_dir": base / "报表",
        "classification_dir": base / "分类结果",
    }


def write_capture_items(layout: dict[str, Path], items: list[dict]) -> Path:
    """把本次采集并入当天的清单（capture-items.json）。

    同一天的多次采集现在共用一个日期目录，所以清单必须**合并**而不是覆盖，
    否则后一次采集会把前一次（另一个方向）的条目整份冲掉。
    以 file_name 为键去重；本次的数据优先（同一文件以最新一次为准）。
    保序：已有序在前，本次新增的追加在后。
    """
    target = Path(layout["report_dir"]) / "capture-items.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    merged: dict[str, dict] = {}
    if target.is_file():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            existing = []
        if isinstance(existing, list):
            for entry in existing:
                if not isinstance(entry, dict):
                    continue
                key = entry.get("file_name") or entry.get("source_url")
                if key:
                    merged[key] = entry

    for entry in items:
        if not isinstance(entry, dict):
            continue
        key = entry.get("file_name") or entry.get("source_url")
        if key:
            merged[key] = entry

    temp = target.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps(list(merged.values()), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temp.replace(target)
    return target
