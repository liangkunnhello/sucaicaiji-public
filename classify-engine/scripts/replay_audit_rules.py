from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import classify_pure_api as classifier
from api_audit_excel import export_api_audit_xlsx


def load_audit_entries(audit_path: Path) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(audit_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        entry = json.loads(line)
        if not isinstance(entry, dict) or not str(entry.get("file", "")).strip():
            raise ValueError(f"审计第 {line_number} 行缺少文件名")
        file_name = str(entry["file"])
        if file_name in entries:
            raise ValueError(f"审计存在重复文件名：{file_name}")
        entries[file_name] = entry
    return entries


def replay_evidence(entry: dict[str, Any], image: Path) -> dict[str, Any] | None:
    """从审计条目还原视觉证据；这条审计没有可用证据时返回 None。

    返回 None 而不是抛异常：审计里只要有一条 API 失败的记录（没有
    api_response_json / keywords），整个批次就再也重放不了 —— 应该跳过它、
    把剩下能重放的跑完，并在结尾报告跳过了多少张。
    """
    source = entry.get("api_response_json") or entry.get("keywords")
    if not isinstance(source, dict):
        return None
    evidence = dict(source)
    evidence["_cached"] = True
    evidence["_image_name"] = image.name
    image_width, image_height = classifier.read_image_dimensions(image)
    evidence["_image_width"] = image_width
    evidence["_image_height"] = image_height
    evidence["_api_response_raw"] = str(
        entry.get("api_response_raw")
        or json.dumps(source, ensure_ascii=False)
    )
    if isinstance(entry.get("api_meta"), dict):
        evidence["_api_meta"] = entry["api_meta"]
    return evidence


def classify_for_replay(
    evidence: dict[str, Any], min_confidence: float, platform: str
) -> dict[str, Any]:
    """按主流程的分流规则产出分类结果。

    必须和 `classify_pure_api` 主流程一致（见其 `output_mode` 分流）：
      · business_line —— 外部平台（妙思/有米云/热云）输出「业务线/方向」，**不套分类表**；
      · product       —— 内部系统继续走分类表门控。
    以前这里无条件套分类表，导致外部平台被错误门控：
    保险的「上下左右图文 / 宠物 / 朋友圈」全被打成「未知」，
    衍生方向也从「秒思自动衍生」被改名成「妙思衍生」——重放结果与真实跑批不一致。
    """
    classified = classifier.classify_keyword_evidence(evidence, min_confidence, platform)
    if classified.get("output_mode") == "business_line":
        return classified
    return classifier.apply_collection_taxonomy(classified)


def main() -> None:
    parser = argparse.ArgumentParser(description="使用已有视觉审计重放当前本地分类规则")
    parser.add_argument("--source", required=True, help="原始图片目录")
    parser.add_argument("--audit", required=True, help="已有 API 分类审计 JSONL")
    parser.add_argument("--output", required=True, help="新的分类结果目录")
    parser.add_argument("--config", required=True, help="分类配置文件")
    parser.add_argument(
        "--platform",
        choices=sorted(classifier.SUPPORTED_PLATFORMS),
        default="internal",
    )
    parser.add_argument("--min-confidence", type=float, default=0.85)
    args = parser.parse_args()

    source = Path(args.source).resolve()
    audit_path = Path(args.audit).resolve()
    output = Path(args.output).resolve()
    config_path = Path(args.config).resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"原始图片目录不存在：{source}")
    if not audit_path.is_file():
        raise FileNotFoundError(f"审计文件不存在：{audit_path}")
    if not config_path.is_file():
        raise FileNotFoundError(f"分类配置不存在：{config_path}")
    if output.exists():
        raise FileExistsError(f"输出目录已存在，拒绝覆盖：{output}")

    config = json.loads(config_path.read_text(encoding="utf-8"))
    classifier.apply_classification_rule_overrides(config.get("classification_rules", {}))
    entries = load_audit_entries(audit_path)
    images = classifier.scan_images(source, output)
    # 缺审计条目的图片跳过而不是整批中止：批次里混进新图是常态，
    # 不该因为几张没有证据就把整个历史批次的重放卡死。
    missing = [image.name for image in images if image.name not in entries]
    if missing:
        print(f"WARN|{len(missing)} 张图片没有审计条目，将跳过，首张：{missing[0]}")

    output.mkdir(parents=True)
    output_audit = output / "_API分类审计.jsonl"
    auto_count = 0
    review_count = 0
    skipped_count = 0
    skipped_samples: list[str] = []
    with output_audit.open("w", encoding="utf-8") as handle:
        for index, image in enumerate(images, 1):
            source_entry = entries.get(image.name)
            evidence = replay_evidence(source_entry, image) if source_entry else None
            if evidence is None:
                skipped_count += 1
                if len(skipped_samples) < 3:
                    skipped_samples.append(image.name)
                continue
            result = classify_for_replay(evidence, args.min_confidence, args.platform)
            result["api_refresh_requested"] = False
            result["rule_replay"] = True
            result["evidence_prompt_version"] = str(source_entry.get("prompt_version", ""))
            result["prompt_version"] = str(source_entry.get("prompt_version", ""))
            status = "REVIEW" if result.get("abstain") else "AUTO"
            destination = classifier.place_file(
                image,
                classifier.classification_destination(
                    output, result, classifier.image_orientation(image)
                ),
                "copy",
            )
            classifier.write_audit(
                handle,
                image,
                result,
                status,
                destination,
                classifier.image_checkpoint_key(image),
            )
            if status == "AUTO":
                auto_count += 1
            else:
                review_count += 1
            print(f"PROGRESS|{index}|{len(images)}|{status}|{image.name}")

    output_excel = output / "_API分类调试审计.xlsx"
    export_api_audit_xlsx(
        output_audit,
        output_excel,
        {
            "source": str(source),
            "output": str(output),
            "model": str(config.get("model_id", "")),
            "prompt_version": f"规则重放:{next(iter(entries.values())).get('prompt_version', '')}",
            "min_confidence": args.min_confidence,
            "parallel": 0,
            "action": "copy",
            "platform": args.platform,
        },
    )
    print(f"SUMMARY|AUTO={auto_count}|REVIEW={review_count}|TOTAL={len(images)}|SKIPPED={skipped_count}")
    if skipped_count:
        print(
            f"SKIPPED|{skipped_count} 张没有可用视觉证据（API 失败或审计缺失），未参与重放："
            + "、".join(skipped_samples)
        )
    print(f"OUTPUT|{output}")


if __name__ == "__main__":
    main()
