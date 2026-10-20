#!/usr/bin/env python3
"""把 API 分类 JSONL 审计记录导出为便于人工复核的 Excel。"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any


EXCEL_TEXT_LIMIT = 30_000


def _text(value: Any, limit: int = EXCEL_TEXT_LIMIT) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    return text[:limit]


def _items(value: Any) -> str:
    if isinstance(value, list):
        return " | ".join(_text(item, 500) for item in value)
    return _text(value)


def _number(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def load_audit_entries(audit_path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with audit_path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"审计文件第 {line_no} 行不是合法 JSON：{exc}") from exc
            if isinstance(value, dict):
                entries.append(value)
    return entries


def _legacy_api_json(entry: dict[str, Any]) -> dict[str, Any]:
    """旧版审计没有原始响应时，用已保存的关键词重建可读证据。"""
    keywords = entry.get("keywords", {})
    if not isinstance(keywords, dict):
        keywords = {}
    rebuilt = dict(keywords)
    confidence = entry.get("api_evidence_confidence")
    if confidence is not None:
        rebuilt["confidence"] = confidence
    rebuilt["_audit_note"] = "历史记录由已保存关键词重建；新任务会保留 API 原始文本"
    return rebuilt


def _api_json(entry: dict[str, Any]) -> dict[str, Any]:
    value = entry.get("api_response_json")
    return value if isinstance(value, dict) else _legacy_api_json(entry)


def _api_raw(entry: dict[str, Any]) -> str:
    raw = entry.get("api_response_raw")
    return _text(raw if raw else _api_json(entry))


def _image_path(entry: dict[str, Any]) -> Path | None:
    for field in ("destination", "source"):
        value = str(entry.get(field, "")).strip()
        if value:
            path = Path(value)
            if path.is_file():
                return path
    return None


def _thumbnail(image_path: Path, width: int = 200, height: int = 125) -> BytesIO:
    """生成固定画布的 JPEG 缩略图，避免把数百张原图原尺寸塞入工作簿。"""
    from PIL import Image, ImageOps

    with Image.open(image_path) as source:
        image = ImageOps.exif_transpose(source)
        if image.mode in {"RGBA", "LA"}:
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (248, 250, 252, 255))
            background.alpha_composite(rgba)
            image = background.convert("RGB")
        else:
            image = image.convert("RGB")
        image = ImageOps.contain(
            image,
            (width, height),
            method=Image.Resampling.LANCZOS,
        )
        canvas = Image.new("RGB", (width, height), (245, 247, 250))
        x = (width - image.width) // 2
        y = (height - image.height) // 2
        canvas.paste(image, (x, y))
        buffer = BytesIO()
        canvas.save(buffer, format="JPEG", quality=72, optimize=True)
        buffer.seek(0)
        return buffer


def _keyword_summary(entry: dict[str, Any]) -> str:
    keywords = entry.get("keywords", {})
    if not isinstance(keywords, dict):
        return ""
    labels = (
        ("文案", "visible_text"),
        ("品牌", "brand_keywords"),
        ("视觉", "visual_keywords"),
        ("版式", "layout_keywords"),
        ("操作", "action_keywords"),
        ("场景", "scene_keywords"),
        ("不确定", "uncertain"),
    )
    parts = [
        f"{label}：{_items(keywords.get(field, []))}"
        for label, field in labels
        if keywords.get(field)
    ]
    return "\n".join(parts)


def _decision_summary(entry: dict[str, Any]) -> str:
    scores = entry.get("system_scores", {})
    if not isinstance(scores, dict):
        scores = {}
    return (
        f"产品得分：{_number(scores.get('product')):.0f}"
        f"（领先 {_number(scores.get('product_margin')):.0f}）\n"
        f"方向得分：{_number(scores.get('direction')):.0f}"
        f"（领先 {_number(scores.get('direction_margin')):.0f}）\n"
        f"{_text(entry.get('reason'))}"
    )


def _candidate_rows(entry: dict[str, Any], image_index: int) -> list[list[Any]]:
    debug = entry.get("rule_debug", {})
    if not isinstance(debug, dict):
        debug = {}
    output: list[list[Any]] = []
    file_name = _text(entry.get("file"))
    selected_product = _text(entry.get("product"))
    selected_direction = _text(entry.get("direction"))
    scores = entry.get("system_scores", {})
    if not isinstance(scores, dict):
        scores = {}

    layers = (
        (
            "产品",
            debug.get("product_candidates", []),
            selected_product,
            _number(debug.get("product_min_score"), 9),
            _number(scores.get("product_margin")),
        ),
        (
            "方向",
            debug.get("direction_candidates", []),
            selected_direction,
            _number(debug.get("direction_min_score"), 8),
            _number(scores.get("direction_margin")),
        ),
    )
    for layer, candidates, selected, minimum, margin in layers:
        if not isinstance(candidates, list):
            candidates = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            name = _text(candidate.get("name"))
            output.append([
                image_index,
                file_name,
                layer,
                name,
                _number(candidate.get("score")),
                _items(candidate.get("hits", [])),
                "是" if name == selected else "否",
                minimum,
                margin if name == selected else "",
            ])

    # 兼容旧版 JSONL：至少把当时保存的最高分和命中证据展示出来。
    if not output:
        evidence = entry.get("evidence", [])
        if selected_product and selected_product != "待确认":
            output.append([
                image_index,
                file_name,
                "产品",
                selected_product,
                _number(scores.get("product")),
                _items(evidence),
                "是",
                9,
                _number(scores.get("product_margin")),
            ])
        if selected_direction and selected_direction != "待确认":
            output.append([
                image_index,
                file_name,
                "方向",
                selected_direction,
                _number(scores.get("direction")),
                _items(evidence),
                "是",
                8,
                _number(scores.get("direction_margin")),
            ])
    return output


def export_api_audit_xlsx(
    audit_path: Path,
    output_path: Path,
    metadata: dict[str, Any] | None = None,
) -> Path:
    import xlsxwriter

    audit_path = Path(audit_path).resolve()
    output_path = Path(output_path).resolve()
    metadata = metadata or {}
    entries = load_audit_entries(audit_path)
    if not entries:
        raise ValueError("审计记录为空，无法生成 Excel")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    workbook = xlsxwriter.Workbook(
        str(output_path),
        {
            "strings_to_urls": False,
            "strings_to_formulas": False,
            "strings_to_numbers": False,
        },
    )
    workbook.set_properties({
        "title": "API 分类调试审计",
        "subject": "图片、API 关键词证据与本地规则判定对应关系",
        "author": "素材运营台",
        "company": "Material Operations",
        "comments": "由 API 分类调试审计记录自动生成",
    })

    navy = "#17345C"
    blue = "#2457C5"
    teal = "#12866F"
    red = "#C73A46"
    amber = "#B7791F"
    pale_blue = "#EAF1FF"
    pale_green = "#E8F6F1"
    pale_red = "#FCEBEC"
    pale_amber = "#FFF6DE"
    line = "#D8E0EA"
    ink = "#24344D"
    muted = "#69778C"
    white = "#FFFFFF"

    title_fmt = workbook.add_format({
        "bold": True, "font_size": 20, "font_color": white,
        "bg_color": navy, "align": "left", "valign": "vcenter",
    })
    subtitle_fmt = workbook.add_format({
        "font_size": 10, "font_color": "#D8E5F7",
        "bg_color": navy, "align": "left", "valign": "vcenter",
    })
    section_fmt = workbook.add_format({
        "bold": True, "font_size": 11, "font_color": navy,
        "bg_color": "#E7EDF5", "bottom": 2, "bottom_color": blue,
        "align": "left", "valign": "vcenter",
    })
    label_fmt = workbook.add_format({
        "bold": True, "font_color": muted, "bg_color": "#F5F7FA",
        "border": 1, "border_color": line, "align": "left", "valign": "vcenter",
    })
    value_fmt = workbook.add_format({
        "font_color": ink, "bg_color": white,
        "border": 1, "border_color": line, "align": "left", "valign": "vcenter",
        "text_wrap": True,
    })
    kpi_label_fmt = workbook.add_format({
        "bold": True, "font_color": muted, "bg_color": "#F5F7FA",
        "top": 1, "left": 1, "right": 1, "border_color": line,
        "align": "center", "valign": "vcenter",
    })
    kpi_value_fmt = workbook.add_format({
        "bold": True, "font_size": 18, "font_color": blue, "bg_color": white,
        "bottom": 1, "left": 1, "right": 1, "border_color": line,
        "align": "center", "valign": "vcenter", "num_format": "#,##0",
    })
    kpi_percent_fmt = workbook.add_format({
        "bold": True, "font_size": 18, "font_color": teal, "bg_color": white,
        "bottom": 1, "left": 1, "right": 1, "border_color": line,
        "align": "center", "valign": "vcenter", "num_format": "0.0%",
    })
    header_fmt = workbook.add_format({
        "bold": True, "font_color": white, "bg_color": navy,
        "border": 1, "border_color": "#5B7190",
        "align": "center", "valign": "vcenter", "text_wrap": True,
    })
    body_fmt = workbook.add_format({
        "font_color": ink, "border": 1, "border_color": line,
        "align": "left", "valign": "top",
    })
    wrap_fmt = workbook.add_format({
        "font_color": ink, "border": 1, "border_color": line,
        "align": "left", "valign": "top", "text_wrap": True,
    })
    center_fmt = workbook.add_format({
        "font_color": ink, "border": 1, "border_color": line,
        "align": "center", "valign": "vcenter",
    })
    number_fmt = workbook.add_format({
        "font_color": ink, "border": 1, "border_color": line,
        "align": "right", "valign": "vcenter", "num_format": "0",
    })
    confidence_fmt = workbook.add_format({
        "font_color": ink, "border": 1, "border_color": line,
        "align": "right", "valign": "vcenter", "num_format": "0.0%",
    })
    link_fmt = workbook.add_format({
        "font_color": blue, "underline": True,
        "border": 1, "border_color": line,
        "align": "center", "valign": "vcenter",
    })
    note_fmt = workbook.add_format({
        "font_color": muted, "italic": True, "text_wrap": True,
        "align": "left", "valign": "top",
    })

    # 1) 摘要
    summary = workbook.add_worksheet("摘要")
    summary.hide_gridlines(2)
    summary.set_tab_color(blue)
    summary.set_column("A:A", 24)
    summary.set_column("B:D", 18)
    summary.set_column("E:E", 3)
    summary.set_column("F:M", 14)
    summary.set_row(0, 36)
    summary.set_row(1, 24)
    summary.merge_range("A1:M1", "API 分类调试审计", title_fmt)
    summary.merge_range(
        "A2:M2",
        "逐图核对：原始图片 → API 关键词证据 → 本地规则分数 → 最终归档",
        subtitle_fmt,
    )

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    meta_rows = [
        ("生成时间", generated_at),
        ("源图片目录", metadata.get("source", "")),
        ("分类输出目录", metadata.get("output", str(output_path.parent))),
        ("模型", metadata.get("model", "")),
        ("提示词版本", metadata.get("prompt_version", "")),
        ("最低置信度", metadata.get("min_confidence", "")),
        ("并发 / 文件动作", f"{metadata.get('parallel', '')} / {metadata.get('action', '')}"),
        ("原始审计文件", str(audit_path)),
    ]
    summary.merge_range("A4:D4", "任务信息", section_fmt)
    for offset, (label, value) in enumerate(meta_rows, 4):
        summary.write(offset, 0, label, label_fmt)
        summary.merge_range(offset, 1, offset, 3, _text(value), value_fmt)
    summary.set_row(11, 34)

    total = len(entries)
    auto_count = sum(1 for entry in entries if entry.get("status") == "AUTO")
    review_count = total - auto_count
    cached_count = sum(1 for entry in entries if bool(entry.get("cached")))
    average_confidence = (
        sum(_number(entry.get("confidence")) for entry in entries) / total
        if total else 0.0
    )
    detail_last_row = total + 1
    kpis = [
        ("总图片数", f"=COUNTA('判定明细'!$B$2:$B${detail_last_row})", total, kpi_value_fmt),
        ("自动分类", f'=COUNTIF(\'判定明细\'!$D$2:$D${detail_last_row},"AUTO")', auto_count, kpi_value_fmt),
        ("待确认", f'=COUNTIF(\'判定明细\'!$D$2:$D${detail_last_row},"REVIEW")', review_count, kpi_value_fmt),
        ("平均最终置信度", f"=IFERROR(AVERAGE('判定明细'!$G$2:$G${detail_last_row}),0)", average_confidence, kpi_percent_fmt),
    ]
    for index, (label, formula, cached_value, fmt) in enumerate(kpis):
        col = 5 + index * 2
        summary.merge_range(3, col, 3, col + 1, label, kpi_label_fmt)
        summary.merge_range(4, col, 5, col + 1, "", fmt)
        summary.write_formula(4, col, formula, fmt, cached_value)
    summary.merge_range("F7:G7", "缓存命中", kpi_label_fmt)
    summary.merge_range("F8:G9", "", kpi_value_fmt)
    summary.write_formula(
        "F8",
        f'=COUNTIF(\'判定明细\'!$X$2:$X${detail_last_row},"是")',
        kpi_value_fmt,
        cached_count,
    )
    summary.merge_range(
        "H7:M9",
        "绿色=AUTO 自动归档；红色=REVIEW 待确认；黄色=候选得分未过阈值、领先不足或置信度不足。",
        workbook.add_format({
            "font_color": ink, "bg_color": pale_amber,
            "border": 1, "border_color": "#E9CF86",
            "text_wrap": True, "align": "left", "valign": "vcenter",
        }),
    )

    product_counts = Counter(_text(entry.get("product") or "待确认") for entry in entries)
    product_review_counts = Counter(
        _text(entry.get("product") or "待确认")
        for entry in entries
        if entry.get("status") != "AUTO"
    )
    product_start = 13
    summary.merge_range(product_start - 1, 0, product_start - 1, 3, "产品分布", section_fmt)
    distribution_headers = ["产品", "图片数", "待确认数", "待确认率"]
    for col, header in enumerate(distribution_headers):
        summary.write(product_start, col, header, header_fmt)
    for offset, (product, count) in enumerate(product_counts.most_common(), product_start + 1):
        review = product_review_counts[product]
        summary.write(offset, 0, product, body_fmt)
        summary.write_formula(
            offset,
            1,
            f'=COUNTIF(\'判定明细\'!$E$2:$E${detail_last_row},A{offset + 1})',
            number_fmt,
            count,
        )
        summary.write_formula(
            offset,
            2,
            f'=COUNTIFS(\'判定明细\'!$E$2:$E${detail_last_row},A{offset + 1},'
            f'\'判定明细\'!$D$2:$D${detail_last_row},"REVIEW")',
            number_fmt,
            review,
        )
        summary.write_formula(
            offset,
            3,
            f"=IFERROR(C{offset + 1}/B{offset + 1},0)",
            confidence_fmt,
            review / count if count else 0.0,
        )
    distribution_last = product_start + len(product_counts)
    summary.conditional_format(
        product_start + 1, 3, distribution_last, 3,
        {"type": "3_color_scale", "min_color": pale_green, "mid_color": pale_amber, "max_color": pale_red},
    )
    if product_counts:
        chart = workbook.add_chart({"type": "bar"})
        chart.add_series({
            "name": "图片数",
            "categories": f"='摘要'!$A${product_start + 2}:$A${distribution_last + 1}",
            "values": f"='摘要'!$B${product_start + 2}:$B${distribution_last + 1}",
            "fill": {"color": blue},
            "border": {"none": True},
        })
        chart.set_title({"name": "各产品图片数量"})
        chart.set_legend({"none": True})
        chart.set_x_axis({"name": "图片数", "major_gridlines": {"visible": False}})
        chart.set_y_axis({"reverse": True})
        chart.set_style(10)
        chart.set_size({"width": 650, "height": 330})
        summary.insert_chart("F13", chart)
    summary.freeze_panes(3, 0)

    # 2) 图片对照：缩略图、API 证据和规则判定保持同一行。
    image_sheet = workbook.add_worksheet("图片对照")
    image_sheet.hide_gridlines(2)
    image_sheet.set_tab_color("#6C4EC7")
    image_headers = [
        "序号", "图片预览", "图片文件", "状态", "最终产品", "最终方向",
        "最终置信度", "API关键词证据", "本地规则判定", "命中规则", "图片路径",
    ]
    image_widths = [8, 31, 43, 11, 22, 20, 12, 68, 58, 44, 62]
    for col, width in enumerate(image_widths):
        image_sheet.set_column(col, col, width)
        image_sheet.write(0, col, image_headers[col], header_fmt)
    image_sheet.set_row(0, 32)
    image_buffers: list[BytesIO] = []
    embedded_count = 0
    missing_count = 0
    for row, entry in enumerate(entries, 1):
        image_path = _image_path(entry)
        image_sheet.write_number(row, 0, row, number_fmt)
        image_sheet.write_blank(row, 1, None, body_fmt)
        image_sheet.write(row, 2, _text(entry.get("file")), wrap_fmt)
        image_sheet.write(row, 3, _text(entry.get("status")), center_fmt)
        image_sheet.write(row, 4, _text(entry.get("product")), center_fmt)
        image_sheet.write(row, 5, _text(entry.get("direction")), center_fmt)
        image_sheet.write_number(row, 6, _number(entry.get("confidence")), confidence_fmt)
        image_sheet.write(row, 7, _keyword_summary(entry), wrap_fmt)
        image_sheet.write(row, 8, _decision_summary(entry), wrap_fmt)
        image_sheet.write(row, 9, _items(entry.get("evidence", [])), wrap_fmt)
        image_sheet.write(row, 10, str(image_path or ""), wrap_fmt)
        image_sheet.set_row(row, 100)
        if image_path is None:
            image_sheet.write(
                row,
                1,
                "图片不存在",
                workbook.add_format({
                    "bg_color": pale_red,
                    "font_color": red,
                    "bold": True,
                    "border": 1,
                    "border_color": line,
                    "align": "center",
                    "valign": "vcenter",
                }),
            )
            missing_count += 1
            continue
        try:
            image_data = _thumbnail(image_path)
            image_buffers.append(image_data)
            image_sheet.insert_image(
                row,
                1,
                image_path.name,
                {
                    "image_data": image_data,
                    "x_offset": 7,
                    "y_offset": 4,
                    "object_position": 1,
                    "description": _text(entry.get("file")) or image_path.name,
                },
            )
            embedded_count += 1
        except Exception as exc:
            image_sheet.write(
                row,
                1,
                f"缩略图失败\n{_text(exc, 180)}",
                workbook.add_format({
                    "bg_color": pale_red,
                    "font_color": red,
                    "border": 1,
                    "border_color": line,
                    "text_wrap": True,
                    "align": "center",
                    "valign": "vcenter",
                }),
            )
            missing_count += 1
    image_sheet.add_table(
        0, 0, len(entries), len(image_headers) - 1,
        {
            "name": "ImageComparisonTable",
            "style": "Table Style Medium 2",
            "columns": [{"header": header} for header in image_headers],
        },
    )
    image_sheet.freeze_panes(1, 3)
    image_sheet.conditional_format(
        1, 3, len(entries), 3,
        {"type": "text", "criteria": "containing", "value": "AUTO",
         "format": workbook.add_format({"bg_color": pale_green, "font_color": teal, "bold": True})},
    )
    image_sheet.conditional_format(
        1, 3, len(entries), 3,
        {"type": "text", "criteria": "containing", "value": "REVIEW",
         "format": workbook.add_format({"bg_color": pale_red, "font_color": red, "bold": True})},
    )
    image_sheet.conditional_format(
        1, 6, len(entries), 6,
        {"type": "data_bar", "bar_color": blue, "bar_solid": True},
    )
    image_sheet.set_footer(
        f"&L嵌入缩略图 {embedded_count} 张"
        f"{f'，缺失/失败 {missing_count} 张' if missing_count else ''}"
        "&R第 &P / &N 页"
    )

    # 3) 判定明细
    details = workbook.add_worksheet("判定明细")
    details.hide_gridlines(2)
    details.set_tab_color(teal)
    detail_headers = [
        "序号", "图片文件", "查看图片", "状态", "最终产品", "最终方向",
        "最终置信度", "API证据置信度", "产品规则置信度", "方向规则置信度",
        "产品最高分", "产品领先分", "方向最高分", "方向领先分",
        "判定原因", "命中规则证据", "可见文字", "品牌关键词", "视觉关键词",
        "版式关键词", "操作关键词", "场景关键词", "不确定证据", "是否缓存",
        "原图路径", "归档路径", "分类层级",
    ]
    detail_widths = [
        8, 42, 11, 11, 22, 20, 12, 14, 14, 14, 11, 11, 11, 11,
        48, 42, 54, 30, 42, 38, 32, 34, 30, 11, 62, 62, 16,
    ]
    for col, width in enumerate(detail_widths):
        details.set_column(col, col, width)
    details.set_row(0, 32)
    for col, header in enumerate(detail_headers):
        details.write(0, col, header, header_fmt)

    for row, entry in enumerate(entries, 1):
        keywords = entry.get("keywords", {})
        if not isinstance(keywords, dict):
            keywords = {}
        scores = entry.get("system_scores", {})
        if not isinstance(scores, dict):
            scores = {}
        source_path = _text(entry.get("source"))
        destination = _text(entry.get("destination"))
        link_target = destination or source_path
        api_evidence_confidence = entry.get("api_evidence_confidence")
        values = [
            row,
            _text(entry.get("file")),
            "",
            _text(entry.get("status")),
            _text(entry.get("product")),
            _text(entry.get("direction")),
            _number(entry.get("confidence")),
            _number(api_evidence_confidence) if api_evidence_confidence is not None else "",
            _number(entry.get("product_confidence")),
            _number(entry.get("direction_confidence")),
            _number(scores.get("product")),
            _number(scores.get("product_margin")),
            _number(scores.get("direction")),
            _number(scores.get("direction_margin")),
            _text(entry.get("reason")),
            _items(entry.get("evidence", [])),
            _items(keywords.get("visible_text", [])),
            _items(keywords.get("brand_keywords", [])),
            _items(keywords.get("visual_keywords", [])),
            _items(keywords.get("layout_keywords", [])),
            _items(keywords.get("action_keywords", [])),
            _items(keywords.get("scene_keywords", [])),
            _items(keywords.get("uncertain", [])),
            "是" if bool(entry.get("cached")) else "否",
            source_path,
            destination,
            {
                "complete": "产品和方向已确定",
                "product_only": "仅产品已确定",
                "unresolved": "产品和方向均未知",
            }.get(_text(entry.get("classification_level")), ""),
        ]
        for col, value in enumerate(values):
            if col == 2:
                if link_target:
                    try:
                        details.write_url(row, col, f"external:{link_target}", link_fmt, "打开")
                    except Exception:
                        details.write(row, col, link_target, wrap_fmt)
                else:
                    details.write(row, col, "", center_fmt)
            elif col in {0, 10, 11, 12, 13}:
                details.write_number(row, col, float(value), number_fmt)
            elif col in {6, 7, 8, 9}:
                if value == "":
                    details.write_blank(row, col, None, confidence_fmt)
                else:
                    details.write_number(row, col, float(value), confidence_fmt)
            elif col in {2, 3, 4, 5, 23}:
                details.write(row, col, value, center_fmt)
            elif col >= 14 or col in {1, 24, 25}:
                details.write(row, col, value, wrap_fmt)
            else:
                details.write(row, col, value, body_fmt)
        details.set_row(row, 45)

    details.add_table(
        0, 0, len(entries), len(detail_headers) - 1,
        {
            "name": "DecisionAuditTable",
            "style": "Table Style Medium 2",
            "columns": [{"header": header} for header in detail_headers],
        },
    )
    details.freeze_panes(1, 2)
    details.conditional_format(
        1, 3, len(entries), 3,
        {"type": "text", "criteria": "containing", "value": "AUTO",
         "format": workbook.add_format({"bg_color": pale_green, "font_color": teal, "bold": True})},
    )
    details.conditional_format(
        1, 3, len(entries), 3,
        {"type": "text", "criteria": "containing", "value": "REVIEW",
         "format": workbook.add_format({"bg_color": pale_red, "font_color": red, "bold": True})},
    )
    details.conditional_format(
        1, 6, len(entries), 9,
        {"type": "data_bar", "bar_color": blue, "bar_solid": True},
    )

    # 4) API 原始记录
    raw_sheet = workbook.add_worksheet("API原始记录")
    raw_sheet.hide_gridlines(2)
    raw_sheet.set_tab_color(amber)
    raw_headers = [
        "序号", "图片文件", "API原始文本", "API解析JSON",
        "模型/请求元信息", "提示词版本", "是否缓存",
    ]
    raw_widths = [8, 44, 80, 80, 48, 26, 11]
    for col, width in enumerate(raw_widths):
        raw_sheet.set_column(col, col, width)
        raw_sheet.write(0, col, raw_headers[col], header_fmt)
    raw_sheet.set_row(0, 32)
    for row, entry in enumerate(entries, 1):
        raw_sheet.write_number(row, 0, row, number_fmt)
        raw_sheet.write(row, 1, _text(entry.get("file")), wrap_fmt)
        raw_sheet.write(row, 2, _api_raw(entry), wrap_fmt)
        raw_sheet.write(row, 3, _text(_api_json(entry)), wrap_fmt)
        raw_sheet.write(row, 4, _text(entry.get("api_meta", {})), wrap_fmt)
        raw_sheet.write(row, 5, _text(entry.get("prompt_version") or metadata.get("prompt_version")), center_fmt)
        raw_sheet.write(row, 6, "是" if bool(entry.get("cached")) else "否", center_fmt)
        raw_sheet.set_row(row, 72)
    raw_sheet.add_table(
        0, 0, len(entries), len(raw_headers) - 1,
        {
            "name": "ApiRawAuditTable",
            "style": "Table Style Medium 2",
            "columns": [{"header": header} for header in raw_headers],
        },
    )
    raw_sheet.freeze_panes(1, 2)

    # 5) 规则候选
    candidate_rows: list[list[Any]] = []
    for image_index, entry in enumerate(entries, 1):
        candidate_rows.extend(_candidate_rows(entry, image_index))
    rules_sheet = workbook.add_worksheet("规则候选")
    rules_sheet.hide_gridlines(2)
    rules_sheet.set_tab_color(navy)
    rule_headers = [
        "图片序号", "图片文件", "规则层级", "候选分类", "得分",
        "命中项", "是否最终选择", "最低得分阈值", "最终领先分",
    ]
    rule_widths = [10, 44, 12, 24, 10, 56, 14, 14, 12]
    for col, width in enumerate(rule_widths):
        rules_sheet.set_column(col, col, width)
        rules_sheet.write(0, col, rule_headers[col], header_fmt)
    rules_sheet.set_row(0, 32)
    for row, values in enumerate(candidate_rows, 1):
        for col, value in enumerate(values):
            if col in {0, 4, 7, 8} and value != "":
                rules_sheet.write_number(row, col, float(value), number_fmt)
            elif col in {2, 3, 6}:
                rules_sheet.write(row, col, value, center_fmt)
            else:
                rules_sheet.write(row, col, value, wrap_fmt if col in {1, 5} else body_fmt)
        rules_sheet.set_row(row, 36)
    if candidate_rows:
        rules_sheet.add_table(
            0, 0, len(candidate_rows), len(rule_headers) - 1,
            {
                "name": "RuleCandidateTable",
                "style": "Table Style Medium 2",
                "columns": [{"header": header} for header in rule_headers],
            },
        )
        rules_sheet.conditional_format(
            1, 4, len(candidate_rows), 4,
            {"type": "3_color_scale", "min_color": pale_red, "mid_color": pale_amber, "max_color": pale_green},
        )
        rules_sheet.conditional_format(
            1, 6, len(candidate_rows), 6,
            {"type": "text", "criteria": "containing", "value": "是",
             "format": workbook.add_format({"bg_color": pale_blue, "font_color": blue, "bold": True})},
        )
    else:
        rules_sheet.merge_range(
            "A2:I4",
            "历史记录没有保存候选规则明细；完成下一次 API 分类后会自动记录全部候选得分。",
            note_fmt,
        )
    rules_sheet.freeze_panes(1, 2)

    workbook.close()
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 API 分类调试审计 Excel")
    parser.add_argument("--audit", required=True, help="JSONL 审计文件")
    parser.add_argument("--output", required=True, help="输出 xlsx 文件")
    parser.add_argument("--source", default="", help="源图片目录")
    parser.add_argument("--classification-output", default="", help="分类结果目录")
    parser.add_argument("--model", default="", help="模型 ID")
    parser.add_argument("--prompt-version", default="", help="提示词版本")
    parser.add_argument("--min-confidence", type=float, default=0.0, help="最低置信度")
    parser.add_argument("--parallel", type=int, default=0, help="并发数")
    parser.add_argument("--action", default="", help="文件动作")
    args = parser.parse_args()
    result = export_api_audit_xlsx(
        Path(args.audit),
        Path(args.output),
        {
            "source": args.source,
            "output": args.classification_output,
            "model": args.model,
            "prompt_version": args.prompt_version,
            "min_confidence": args.min_confidence,
            "parallel": args.parallel,
            "action": args.action,
        },
    )
    print(result)


if __name__ == "__main__":
    main()
