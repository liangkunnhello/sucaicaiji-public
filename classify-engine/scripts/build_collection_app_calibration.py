#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import classify_pure_api as classifier


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def read_audit(path: Path) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            entry = json.loads(line)
            file_name = str(entry.get("file") or "").strip()
            if file_name:
                entries[file_name] = entry
    return entries


def truth_labels(root: Path) -> dict[str, tuple[str, str]]:
    labels: dict[str, tuple[str, str]] = {}
    for image in root.rglob("*"):
        if not image.is_file() or image.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        relative_parts = image.relative_to(root).parts
        product = relative_parts[1] if relative_parts and relative_parts[0] == "APP-拉新" else "未知"
        labels[image.name] = (product, image.parent.name)
    return labels


def load_samples(root: Path, audit_path: Path) -> list[dict[str, Any]]:
    audit_entries = read_audit(audit_path)
    labels = truth_labels(root)
    missing = sorted(set(labels) - set(audit_entries))
    if missing:
        raise ValueError(f"有 {len(missing)} 张真值图片缺少审计证据，示例：{missing[:3]}")
    samples: list[dict[str, Any]] = []
    for file_name, (product, direction) in sorted(labels.items()):
        entry = audit_entries[file_name]
        keywords = entry.get("keywords", {})
        if not isinstance(keywords, dict):
            continue
        detected_direction = str(entry.get("detected_direction") or entry.get("direction") or "")
        samples.append({
            "product": product,
            "label": direction,
            "features": classifier.collection_app_calibration_features(
                keywords,
                detected_direction,
            ),
        })
    return samples


def train_model(
    samples: list[dict[str, Any]],
    *,
    minimum_document_frequency: int = 2,
    alpha: float = 0.3,
) -> dict[str, Any]:
    labels = sorted({str(sample["label"]) for sample in samples})
    label_index = {label: index for index, label in enumerate(labels)}
    document_frequency: Counter[str] = Counter()
    for sample in samples:
        document_frequency.update(sample["features"].keys())
    vocabulary = {
        token
        for token, count in document_frequency.items()
        if count >= minimum_document_frequency
    }
    documents = [0 for _ in labels]
    token_counts: dict[str, list[int]] = {
        token: [0 for _ in labels]
        for token in sorted(vocabulary)
    }
    for sample in samples:
        index = label_index[str(sample["label"])]
        documents[index] += 1
        for token, count in sample["features"].items():
            if token in token_counts:
                token_counts[token][index] += int(count)
    totals = [0 for _ in labels]
    for counts in token_counts.values():
        for index, count in enumerate(counts):
            totals[index] += count
    return {
        "version": "collection-app-calibration-20260818-v1",
        "description": "由人工校准 APP 素材提炼的客观证据字符片段计数，不含图片、路径、文件名或样本ID",
        "labels": labels,
        "documents": documents,
        "totals": totals,
        "tokens": token_counts,
        "alpha": alpha,
        "minimum_document_frequency": minimum_document_frequency,
        "detected_direction_weight": 3,
        "minimum_matched": 2,
        "minimum_margin": 0.75,
        "minimum_probability": 0.38,
        "list_fields": list(classifier.APP_CALIBRATION_LIST_FIELDS),
        "structured_fields": list(classifier.APP_CALIBRATION_STRUCTURED_FIELDS),
        "sample_count": len(samples),
        "product_counts": dict(sorted(Counter(str(sample["product"]) for sample in samples).items())),
        "direction_counts": dict(sorted(Counter(str(sample["label"]) for sample in samples).items())),
    }


def predict(model: dict[str, Any], features: dict[str, int]) -> str | None:
    labels = list(model["labels"])
    documents = list(model["documents"])
    totals = list(model["totals"])
    tokens = dict(model["tokens"])
    alpha = float(model["alpha"])
    vocabulary_size = len(tokens)
    document_total = sum(documents)
    scores: list[tuple[float, str]] = []
    import math

    for index, label in enumerate(labels):
        score = math.log((documents[index] + 1) / (document_total + len(labels)))
        denominator = totals[index] + alpha * vocabulary_size
        for token, count in features.items():
            if token not in tokens:
                continue
            score += count * math.log((tokens[token][index] + alpha) / denominator)
        scores.append((score, label))
    return max(scores)[1] if scores else None


def replay_accuracy(model: dict[str, Any], samples: list[dict[str, Any]]) -> tuple[int, int]:
    correct = sum(
        predict(model, sample["features"]) == sample["label"]
        for sample in samples
    )
    return correct, len(samples)


def cross_validation_accuracy(samples: list[dict[str, Any]], folds: int = 5) -> tuple[int, int]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        buckets[str(sample["label"])].append(sample)
    fold_samples: list[list[dict[str, Any]]] = [[] for _ in range(folds)]
    for label_samples in buckets.values():
        for index, sample in enumerate(label_samples):
            fold_samples[index % folds].append(sample)
    correct = 0
    total = 0
    for fold_index in range(folds):
        validation = fold_samples[fold_index]
        training = [
            sample
            for index, values in enumerate(fold_samples)
            if index != fold_index
            for sample in values
        ]
        model = train_model(training)
        for sample in validation:
            predicted = predict(model, sample["features"])
            correct += predicted == sample["label"]
            total += 1
    return correct, total


def main() -> None:
    parser = argparse.ArgumentParser(description="从人工校准结果生成 APP 方向本地词表")
    parser.add_argument("--truth-root", required=True, help="人工校准后的分类结果目录")
    parser.add_argument("--audit", help="API 分类审计 JSONL，默认使用真值目录下文件")
    parser.add_argument(
        "--output",
        default=str(Path(__file__).with_name("collection_app_calibration_v1.json")),
        help="模型输出路径",
    )
    args = parser.parse_args()
    truth_root = Path(args.truth_root).resolve()
    audit_path = Path(args.audit).resolve() if args.audit else truth_root / "_API分类审计.jsonl"
    output_path = Path(args.output).resolve()
    samples = load_samples(truth_root, audit_path)
    model = train_model(samples)
    replay_correct, replay_total = replay_accuracy(model, samples)
    cross_correct, cross_total = cross_validation_accuracy(samples)
    output_path.write_text(
        json.dumps(model, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"样本: {len(samples)}")
    print(f"方向: {model['direction_counts']}")
    print(f"词表: {len(model['tokens'])}")
    print(f"训练集回放: {replay_correct}/{replay_total} ({100 * replay_correct / replay_total:.2f}%)")
    print(f"5折交叉验证: {cross_correct}/{cross_total} ({100 * cross_correct / cross_total:.2f}%)")
    print(f"输出: {output_path}")


if __name__ == "__main__":
    main()
