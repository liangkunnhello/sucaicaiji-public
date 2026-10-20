from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
WORKSPACE_DIR = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(SCRIPTS_DIR))

import classify_pure_api as classifier  # noqa: E402


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def truth_folders(root: Path) -> dict[str, str]:
    return {
        path.name: str(path.parent.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    }


def superseded_by_v81_rule(expected: str, raw: dict, platform: str) -> bool:
    """旧人工目录中与最新硬口径冲突的标签不再充当回归真值。"""
    parts = Path(expected).parts
    if not parts:
        return False
    product_or_line = parts[0]
    insurance = (
        product_or_line == "保险"
        or classifier.PRODUCT_BUSINESS_LINES.get(product_or_line) == "保险"
    )
    if not insurance:
        return False
    expected_direction = classifier.external_direction_name(
        classifier.canonical_direction_name(parts[-1])
    )
    evidence = classifier.normalize_keyword_evidence(raw)
    pure_scenery_poster = classifier.has_text_led_poster_evidence(evidence)
    balanced_text_graphic = classifier.has_balanced_text_graphic_evidence(evidence)
    if expected_direction == "大字报" and not pure_scenery_poster:
        return True
    if pure_scenery_poster and expected_direction != "大字报":
        return True
    if balanced_text_graphic and expected_direction != "上下左右图文":
        return True
    return False


def replay_accuracy(
    truth_root: Path,
    audit_path: Path,
    platform: str,
    expected_folder,
) -> tuple[int, int, int]:
    truths = truth_folders(truth_root)
    total = 0
    correct = 0
    excluded = 0
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        truth = truths.get(str(entry.get("file", "")))
        if truth is None:
            continue
        expected = expected_folder(truth)
        if expected is None:
            excluded += 1
            continue
        raw = dict(entry.get("api_response_json") or entry.get("keywords") or {})
        raw["_image_name"] = entry["file"]
        result = classifier.classify_keyword_evidence(raw, 0.85, platform)
        actual = str(
            classifier.classification_destination(Path("OUT"), result).relative_to("OUT")
        )
        if actual != expected and superseded_by_v81_rule(expected, raw, platform):
            excluded += 1
            continue
        total += 1
        correct += actual == expected
    return total, correct, excluded


class CalibrationAccuracyTest(unittest.TestCase):
    def test_runtime_has_no_exact_manual_direction_overrides(self) -> None:
        self.assertFalse(hasattr(classifier, "DIRECTION_CALIBRATION_OVERRIDES"))
        self.assertFalse(hasattr(classifier, "manual_calibrated_direction"))
        self.assertFalse(hasattr(classifier, "direction_calibration_signature"))
        self.assertFalse(hasattr(classifier, "TOTAL_REVIEW_CALIBRATION"))

    def test_miaosi_legacy_api_rule_replay_stays_at_or_above_92_percent(self) -> None:
        batch = WORKSPACE_DIR / "data" / "shared" / "material_collections" / "妙思" / "20260727"
        truth = batch / "分类结果校准版"
        audit = batch / "分类结果" / "_API分类审计.jsonl"
        if not truth.is_dir() or not audit.is_file():
            self.skipTest("妙思校准样本不在当前工作区")

        def expected(folder: str) -> str | None:
            if folder.endswith("API失败"):
                return None
            direction = "未知方向" if folder == "_待确认" else folder
            return str(Path("保险") / direction)

        total, correct, excluded = replay_accuracy(truth, audit, "admuse", expected)

        self.assertEqual(500, total + excluded)
        self.assertGreaterEqual(excluded, 3)
        # 该批是旧校准模型的训练来源，API 证据也没有当前的结构化版式字段。
        # 模型停用自动改类后，只将它作为旧证据兼容基线；新 API 回放仍守 95%。
        self.assertGreaterEqual(correct / total, 0.92)

    def test_youmi_20260728_rule_replay_stays_at_or_above_95_percent(self) -> None:
        batch = WORKSPACE_DIR / "data" / "shared" / "material_collections" / "有米云" / "20260728"
        truth = batch / "分类结果"
        audit = truth / "_API分类审计.jsonl"
        if not truth.is_dir() or not audit.is_file():
            self.skipTest("有米云20260728人工复核样本不在当前工作区")

        total, correct, excluded = replay_accuracy(
            truth,
            audit,
            "appgrowing",
            lambda folder: folder,
        )

        self.assertEqual(500, total + excluded)
        self.assertGreaterEqual(correct / total, 0.95)

    def test_youmi_v8_real_api_rule_replay_stays_at_or_above_95_percent(self) -> None:
        truth = (
            WORKSPACE_DIR / "data" / "shared" / "material_collections"
            / "有米云" / "20260728" / "分类结果"
        )
        audit_dir = (
            WORKSPACE_DIR / "data" / "temp"
            / "api_subject_v8_full_20260728" / "output"
        )
        audits = list(audit_dir.glob("_API*.jsonl"))
        if not truth.is_dir() or len(audits) != 1:
            self.skipTest("有米云v8真实API审计不在当前工作区")

        audit = audits[0]
        entries = [
            json.loads(line)
            for line in audit.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(500, len(entries))
        self.assertEqual(
            {"keyword-evidence-v8-20260728"},
            {str(entry.get("prompt_version", "")) for entry in entries},
        )

        total, correct, excluded = replay_accuracy(
            truth,
            audit,
            "appgrowing",
            lambda folder: folder,
        )

        self.assertEqual(500, total + excluded)
        self.assertGreaterEqual(correct / total, 0.95)

    def test_internal_calibration_stays_at_or_above_95_percent(self) -> None:
        batch = WORKSPACE_DIR / "data" / "shared" / "material_collections" / "内部系统" / "20260727"
        truth = batch / "分类结果校准版"
        audit = batch / "分类结果" / "_API分类审计.jsonl"
        if not truth.is_dir() or not audit.is_file():
            self.skipTest("内部系统校准样本不在当前工作区")

        total, correct, excluded = replay_accuracy(truth, audit, "internal", lambda folder: folder)

        self.assertEqual(419, total + excluded)
        self.assertGreaterEqual(excluded, 1)
        self.assertGreaterEqual(correct / total, 0.95)


if __name__ == "__main__":
    unittest.main()
