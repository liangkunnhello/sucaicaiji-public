from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import classify_pure_api as classifier  # noqa: E402


def evidence(
    *,
    layout: list[str],
    visual: list[str] | None = None,
    visible_text: list[str] | None = None,
    brand: list[str] | None = None,
    action: list[str] | None = None,
    scene: list[str] | None = None,
) -> dict[str, object]:
    return {
        "visible_text": (
            visible_text
            if visible_text is not None
            else ["泰康百万医疗险", "保险产品请以保险条款为准"]
        ),
        "brand_keywords": brand or [],
        "visual_keywords": visual or [],
        "layout_keywords": layout,
        "action_keywords": action or [],
        "scene_keywords": scene or [],
        "confidence": 0.96,
        "uncertain": [],
        "_image_name": "sample.jpeg.jpg",
    }


class GlobalDerivedRulesTest(unittest.TestCase):
    def test_classification_ui_defaults_expose_prompt_rules_and_protected_logic(self) -> None:
        defaults = classifier.classification_ui_defaults()

        self.assertEqual(classifier.PROMPT_VERSION, defaults["prompt_version"])
        self.assertEqual(classifier.PROMPT, defaults["default_prompt"])
        self.assertIn("business_line_rules", defaults["default_rules"])
        self.assertIn("product_rules", defaults["default_rules"])
        self.assertIn("direction_rules", defaults["default_rules"])
        self.assertGreaterEqual(len(defaults["protected_rules"]), 6)

    def test_classification_ui_config_validates_and_applies_weight_overrides(self) -> None:
        original_business = copy.deepcopy(classifier.BUSINESS_LINE_RULES)
        original_products = copy.deepcopy(classifier.PRODUCT_RULES)
        original_legacy_products = copy.deepcopy(classifier.LEGACY_PRODUCT_RULES)
        original_directions = copy.deepcopy(classifier.DIRECTION_RULES)
        original_legacy_directions = copy.deepcopy(classifier.LEGACY_DIRECTION_RULES)
        candidate = {
            "api_classify_prompt": classifier.PROMPT,
            "classification_rules": {
                "schema_version": classifier.CLASSIFICATION_RULES_SCHEMA_VERSION,
                "business_line_rules": {"保险": [["保险服务", 31]]},
                "product_rules": {
                    "百万医疗险": {
                        "positive": [["百万医疗险", 25]],
                        "negative": [["防癌险", 19]],
                    }
                },
                "direction_rules": {"图标": [["中心图标主导", 27]]},
            },
        }
        try:
            normalized = classifier.validate_classification_ui_config(candidate)
            classifier.apply_classification_rule_overrides(normalized["classification_rules"])

            self.assertEqual([("保险服务", 31)], classifier.BUSINESS_LINE_RULES["保险"])
            self.assertEqual([("百万医疗险", 25)], classifier.PRODUCT_RULES["百万医疗险"]["positive"])
            self.assertEqual(classifier.PRODUCT_RULES["百万医疗险"], classifier.LEGACY_PRODUCT_RULES["百万医疗险"])
            self.assertEqual([("中心图标主导", 27)], classifier.DIRECTION_RULES["图标"])
            self.assertEqual(classifier.DIRECTION_RULES["图标"], classifier.LEGACY_DIRECTION_RULES["图标"])
        finally:
            classifier.BUSINESS_LINE_RULES.clear()
            classifier.BUSINESS_LINE_RULES.update(original_business)
            classifier.PRODUCT_RULES.clear()
            classifier.PRODUCT_RULES.update(original_products)
            classifier.LEGACY_PRODUCT_RULES.clear()
            classifier.LEGACY_PRODUCT_RULES.update(original_legacy_products)
            classifier.DIRECTION_RULES.clear()
            classifier.DIRECTION_RULES.update(original_directions)
            classifier.LEGACY_DIRECTION_RULES.clear()
            classifier.LEGACY_DIRECTION_RULES.update(original_legacy_directions)

    def test_saving_builtin_rule_snapshot_preserves_legacy_compatibility_weights(self) -> None:
        original_legacy_products = copy.deepcopy(classifier.LEGACY_PRODUCT_RULES)
        original_legacy_directions = copy.deepcopy(classifier.LEGACY_DIRECTION_RULES)

        classifier.apply_classification_rule_overrides(
            classifier.configurable_classification_rules()
        )

        self.assertEqual(original_legacy_products, classifier.LEGACY_PRODUCT_RULES)
        self.assertEqual(original_legacy_directions, classifier.LEGACY_DIRECTION_RULES)

    def test_classification_ui_config_rejects_unknown_products_and_incomplete_prompts(self) -> None:
        with self.assertRaisesRegex(ValueError, "缺少必需字段"):
            classifier.validate_classification_ui_config({
                "api_classify_prompt": "只返回 visible_text",
                "classification_rules": {},
            })
        with self.assertRaisesRegex(ValueError, "不支持新增产品"):
            classifier.validate_classification_ui_config({
                "api_classify_prompt": classifier.PROMPT,
                "classification_rules": {
                    "product_rules": {
                        "不存在的产品": {"positive": [["测试", 10]], "negative": []},
                    }
                },
            })

    def test_global_direction_is_available_to_every_product(self) -> None:
        for product, directions in classifier.PRODUCT_DIRECTIONS.items():
            with self.subTest(product=product):
                self.assertIn("秒思自动衍生", directions)

    def test_merged_direction_names_are_removed_from_all_product_candidates(self) -> None:
        removed = {"家庭亲子插画", "盾牌", "盾牌锁"}

        self.assertTrue(removed.isdisjoint(classifier.DIRECTIONS))
        self.assertTrue(removed.isdisjoint(classifier.DIRECTION_RULES))
        self.assertTrue(removed.isdisjoint(classifier.LEGACY_DIRECTION_RULES))
        for product, directions in classifier.PRODUCT_DIRECTIONS.items():
            with self.subTest(product=product):
                self.assertTrue(removed.isdisjoint(directions))

    def test_family_parent_child_illustration_is_merged_into_illustration(self) -> None:
        result = classifier.classify_keyword_evidence(
            evidence(
                layout=["插画主导"],
                visual=["家庭亲子插画", "父母和儿童", "卡通插画"],
            ),
            min_confidence=0.85,
        )

        self.assertEqual("百万医疗险", result["product"])
        self.assertEqual("插画", result["direction"])
        self.assertFalse(result["abstain"])

    def test_shield_is_merged_into_icon_for_multiple_products(self) -> None:
        products = (
            (["泰康百万医疗险"], [], "百万医疗险"),
            (["QQ阅读", "权益月月领"], [], "QQ阅读"),
        )

        for visible_text, brand, expected_product in products:
            with self.subTest(product=expected_product):
                result = classifier.classify_keyword_evidence(
                    evidence(
                        layout=["图标主导"],
                        visual=["大型盾牌", "盾牌主导", "盾牌图标"],
                        visible_text=visible_text,
                        brand=brand,
                    ),
                    min_confidence=0.85,
                )

                self.assertEqual(expected_product, result["product"])
                self.assertEqual("图标", result["direction"])
                self.assertFalse(result["abstain"])

    def test_complete_embedded_ad_keeps_medical_product(self) -> None:
        result = classifier.classify_keyword_evidence(
            evidence(
                layout=["完整原素材嵌入", "嵌套广告截图", "二次组合模板"],
                visual=["锁形图标", "存钱罐", "金币"],
            ),
            min_confidence=0.85,
        )

        self.assertEqual("百万医疗险", result["product"])
        self.assertEqual("秒思自动衍生", result["direction"])
        self.assertFalse(result["abstain"])
        self.assertTrue(result["rule_debug"]["derived_nested_hint"])

    def test_complete_embedded_ad_is_derived_for_multiple_products(self) -> None:
        products = {
            "QQ阅读": (["QQ阅读", "权益月月领"], []),
            "快手极速版": (["快手极速版"], ["快手Logo", "闪电角标"]),
            "淘宝联盟": (["淘宝联盟"], []),
            "百万防癌险": (["百万防癌险", "癌症保障"], []),
        }

        for expected_product, (visible_text, brand) in products.items():
            with self.subTest(product=expected_product):
                result = classifier.classify_keyword_evidence(
                    evidence(
                        layout=["完整原素材嵌入", "二次组合模板"],
                        visual=["外层新增营销元素"],
                        visible_text=visible_text,
                        brand=brand,
                    ),
                    min_confidence=0.85,
                )

                self.assertEqual(expected_product, result["product"])
                self.assertEqual("秒思自动衍生", result["direction"])
                self.assertFalse(result["abstain"])
                self.assertTrue(result["rule_debug"]["derived_nested_hint"])

    def test_vertical_standalone_icon_is_not_derived(self) -> None:
        result = classifier.classify_keyword_evidence(
            evidence(
                layout=["图标主导"],
                visual=["锁形图标"],
            ),
            min_confidence=0.85,
        )

        self.assertEqual("图标", result["direction"])
        self.assertFalse(result["rule_debug"]["derived_nested_hint"])

    def test_plain_card_or_frame_is_not_enough_to_count_as_embedded(self) -> None:
        result = classifier.classify_keyword_evidence(
            evidence(
                layout=["图标主导", "卡片界面", "矩形边框"],
                visual=["锁形图标"],
            ),
            min_confidence=0.85,
        )

        self.assertEqual("图标", result["direction"])
        self.assertFalse(result["rule_debug"]["derived_nested_hint"])

    def test_known_product_with_unknown_direction_goes_to_product_folder(self) -> None:
        result = classifier.classify_keyword_evidence(
            evidence(
                layout=["居中构图"],
                visual=["抽象装饰图形"],
            ),
            min_confidence=0.85,
        )

        self.assertEqual("百万医疗险", result["product"])
        self.assertEqual("未知方向", result["direction"])
        self.assertFalse(result["abstain"])
        self.assertTrue(result["partial"])
        self.assertEqual("product_only", result["classification_level"])
        self.assertEqual(
            Path("output") / "百万医疗险" / "未知方向",
            classifier.classification_destination(Path("output"), result),
        )

    def test_unknown_product_and_direction_still_go_to_review(self) -> None:
        result = classifier.classify_keyword_evidence(
            evidence(
                layout=["居中构图"],
                visual=["抽象装饰图形"],
                visible_text=["美好生活，从现在开始"],
            ),
            min_confidence=0.85,
        )

        self.assertEqual("待确认", result["product"])
        self.assertEqual("待确认", result["direction"])
        self.assertTrue(result["abstain"])
        self.assertFalse(result["partial"])
        self.assertEqual("unresolved", result["classification_level"])
        self.assertEqual(
            Path("output") / "_待确认",
            classifier.classification_destination(Path("output"), result),
        )

    def test_overlapping_products_with_complete_names_are_distinguished(self) -> None:
        cases = (
            ("百度", ["百度App", "百度一下"], ["蓝色du熊掌Logo"], []),
            ("百度网盘", ["百度网盘", "网盘超级会员"], ["百度网盘Logo"], ["文件列表", "云存储"]),
            ("百度极速版", ["百度极速版"], ["du熊掌Logo", "红色极速角标"], []),
            ("快手", ["快手App"], ["快手Logo", "橙色相机Logo"], []),
            ("快手极速版", ["快手极速版"], ["快手Logo", "闪电角标"], []),
        )

        for expected, visible_text, brand, visual in cases:
            with self.subTest(product=expected):
                result = classifier.classify_keyword_evidence(
                    evidence(
                        layout=["居中构图"],
                        visual=visual,
                        visible_text=visible_text,
                        brand=brand,
                    ),
                    min_confidence=0.85,
                )

                self.assertEqual(expected, result["product"])
                self.assertFalse(result["abstain"])

    def test_generic_baidu_or_kuaishou_evidence_does_not_force_a_product(self) -> None:
        cases = (
            (["百度"], ["蓝色du熊掌Logo"]),
            (["快手"], ["快手Logo"]),
            (["领现金"], ["红色极速角标"]),
            ([], ["闪电角标"]),
        )

        for visible_text, brand in cases:
            with self.subTest(visible_text=visible_text, brand=brand):
                result = classifier.classify_keyword_evidence(
                    evidence(
                        layout=["居中构图"],
                        visible_text=visible_text,
                        brand=brand,
                    ),
                    min_confidence=0.85,
                )

                self.assertEqual("待确认", result["product"])
                self.assertTrue(result["abstain"])

    def test_overlapping_product_name_alone_is_not_enough(self) -> None:
        product_names = ("百度App", "百度网盘", "百度极速版", "快手App")

        for product_name in product_names:
            with self.subTest(product_name=product_name):
                result = classifier.classify_keyword_evidence(
                    evidence(
                        layout=["居中构图"],
                        visible_text=[product_name],
                    ),
                    min_confidence=0.85,
                )

                self.assertEqual("待确认", result["product"])
                self.assertTrue(result["abstain"])

    def test_family_and_version_specific_evidence_can_confirm_speed_editions(self) -> None:
        cases = (
            ("百度极速版", ["百度"], ["du熊掌Logo", "红色极速角标"]),
            ("快手极速版", ["快手"], ["快手Logo", "闪电角标"]),
        )

        for expected, visible_text, brand in cases:
            with self.subTest(product=expected):
                result = classifier.classify_keyword_evidence(
                    evidence(
                        layout=["居中构图"],
                        visible_text=visible_text,
                        brand=brand,
                    ),
                    min_confidence=0.85,
                )

                self.assertEqual(expected, result["product"])
                self.assertFalse(result["abstain"])

    def test_conflicting_edition_evidence_uses_negative_score_competition(self) -> None:
        cases = (
            ("百度网盘", ["百度App", "百度网盘"], ["百度网盘Logo"]),
            ("百度极速版", ["百度App", "百度极速版"], ["红色极速角标"]),
            ("快手极速版", ["快手App", "快手极速版"], ["闪电角标"]),
        )

        for expected, visible_text, brand in cases:
            with self.subTest(visible_text=visible_text):
                result = classifier.classify_keyword_evidence(
                    evidence(
                        layout=["居中构图"],
                        visible_text=visible_text,
                        brand=brand,
                    ),
                    min_confidence=0.85,
                )

                self.assertEqual(expected, result["product"])
                self.assertFalse(result["abstain"])
                self.assertTrue(result["rule_debug"]["conflict"])
                self.assertEqual("正负分竞争", result["rule_debug"]["conflict_resolution"])

    def test_conflicting_insurance_products_compete_by_score_and_margin(self) -> None:
        cases = (
            (
                "百万医疗险",
                ["百万医疗险", "住院医疗", "免赔额", "百万防癌险"],
            ),
            (
                "百万防癌险",
                ["百万防癌险", "恶性肿瘤", "癌症保障", "百万医疗险"],
            ),
            (
                "待确认",
                ["百万医疗险", "百万防癌险", "癌症", "保障为你护航"],
            ),
        )

        for expected, visible_text in cases:
            with self.subTest(expected=expected):
                result = classifier.classify_keyword_evidence(
                    evidence(
                        layout=["居中构图"],
                        visible_text=visible_text,
                    ),
                    min_confidence=0.85,
                )

                self.assertEqual(expected, result["product"])
                self.assertTrue(result["rule_debug"]["conflict"])
                self.assertEqual("正负分竞争", result["rule_debug"]["conflict_resolution"])
                if expected == "待确认":
                    self.assertTrue(result["abstain"])
                    self.assertLess(result["system_scores"]["product_margin"], 3)
                else:
                    self.assertFalse(result["abstain"])

    def test_prompt_defines_embedded_image_and_counterexamples(self) -> None:
        self.assertIn("完整原素材嵌入", classifier.PROMPT)
        self.assertIn("设备屏幕", classifier.PROMPT)
        self.assertIn("空间层级", classifier.PROMPT)
        self.assertIn("单个锁、盾牌等图标", classifier.PROMPT)
        self.assertIn("竖版比例", classifier.PROMPT)
        self.assertIn("外层画布+内层完整广告", classifier.PROMPT)
        self.assertIn("左侧或上方是一张", classifier.PROMPT)
        self.assertIn("人物头像、五星评分", classifier.PROMPT)
        self.assertIn("插画场景第一视觉主体", classifier.PROMPT)
        self.assertIn("写实人物照片与文字左右或上下并列", classifier.PROMPT)
        self.assertIn("月亮球体第一视觉主体", classifier.PROMPT)
        self.assertIn("文字第一视觉主体", classifier.PROMPT)
        self.assertIn("均衡图文版式", classifier.PROMPT)
        self.assertIn("composition_relation", classifier.PROMPT)
        self.assertIn("composition_structure", classifier.PROMPT)
        self.assertIn("background_type", classifier.PROMPT)
        self.assertIn("纯自然风景", classifier.PROMPT)
        self.assertIn("城市风景", classifier.PROMPT)
        self.assertIn("纯色/简单抽象", classifier.PROMPT)
        self.assertIn("大型球体同时带有圆环", classifier.PROMPT)
        self.assertIn("弯月、月牙、星星只出现在背景", classifier.PROMPT)
        self.assertIn("复古机器、街机、售货机", classifier.PROMPT)
        self.assertIn("text_overlay_on_scenery", classifier.PROMPT)
        self.assertIn("competing_visual_subject", classifier.PROMPT)
        self.assertIn("禁止输出“中心主体+多元素信息场景”", classifier.PROMPT)

    def test_v81_controlled_structure_and_dominance_markers_drive_direction(self) -> None:
        cases = (
            (
                ["外层画布+内层完整广告", "完整原素材嵌入", "二次组合模板"],
                ["蓝色锁图标", "白色广告卡片"],
                "秒思自动衍生",
            ),
            (["单层画布", "插画场景第一视觉主体", "上下分栏"], ["卡通男孩", "发光满月"], "插画"),
            (["单层画布", "月亮球体第一视觉主体", "图标主导"], ["中心渐变球体", "同心圆"], "月亮"),
            (["单层画布", "中心图标第一视觉主体"], ["大型盾牌", "锁形图标"], "图标"),
            (["单层画布", "文字第一视觉主体"], ["森林背景"], "大字报"),
            (["单层画布", "均衡图文版式", "左右分栏"], ["人物照片"], "上下左右图文"),
        )
        for layout, visual, expected in cases:
            with self.subTest(expected=expected):
                raw = evidence(
                    visible_text=["百万医疗险", "保险产品详见保险条款"],
                    layout=layout,
                    visual=visual,
                )
                raw["_image_name"] = "[保险服务]_v8-controlled-evidence.jpg"
                result = classifier.classify_keyword_evidence(raw, 0.85, "appgrowing")
                self.assertEqual(expected, result["direction"])

    def test_admuse_uses_business_line_instead_of_specific_product(self) -> None:
        raw = evidence(
            layout=["图标主导"],
            visual=["蓝色盾牌", "盾牌图标"],
            visible_text=["保障责任详见保险条款"],
        )
        raw["_image_name"] = "[保险服务]_sample.jpg"

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("保险", result["business_line"])
        self.assertEqual("图标", result["direction"])
        self.assertEqual("business_line", result["output_mode"])
        self.assertEqual(
            Path("output") / "保险" / "图标",
            classifier.classification_destination(Path("output"), result),
        )

    def test_reyun_insurance_uses_shared_external_direction_rules(self) -> None:
        raw = evidence(
            visible_text=["百万防癌险", "保险产品详见保险条款"],
            visual=["卡通插画", "家庭三代人物", "发光月亮"],
            layout=["图文结合", "底部大字标题"],
            scene=["中秋家庭团聚场景"],
        )
        raw["_image_name"] = "[保险服务]_reyun-family.jpg"

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="reyun")

        self.assertEqual("保险", result["business_line"])
        self.assertEqual("插画", result["direction"])
        self.assertEqual("business_line", result["output_mode"])
        self.assertEqual(
            Path("output") / "保险" / "插画",
            classifier.classification_destination(Path("output"), result),
        )

    def test_reyun_non_insurance_keeps_product_output(self) -> None:
        raw = evidence(
            visible_text=["QQ阅读", "权益月月领"],
            visual=["大型盾牌", "盾牌主导", "盾牌图标"],
            layout=["图标主导"],
        )

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="reyun")

        self.assertEqual("QQ阅读", result["product"])
        self.assertEqual("product", result["output_mode"])
        self.assertEqual("reyun", result["platform"])

    def test_external_known_business_with_unknown_direction_is_kept(self) -> None:
        raw = evidence(
            layout=["居中构图"],
            visual=["抽象装饰图形"],
            visible_text=["保险产品详见保险条款"],
        )
        raw["_image_name"] = "[保险服务]_sample.jpg"

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="appgrowing")

        self.assertEqual("保险", result["business_line"])
        self.assertEqual("未知方向", result["direction"])
        self.assertFalse(result["abstain"])
        self.assertTrue(result["partial"])

    def test_external_pet_direction_outranks_generic_cartoon(self) -> None:
        raw = evidence(
            layout=["大号文字主导"],
            visual=["粉色小狗", "绿色小猫", "卡通插画", "爪印图案"],
            visible_text=["宠物保障"],
        )
        raw["_image_name"] = "[保险服务]_pet.jpg"

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("保险", result["business_line"])
        self.assertEqual("宠物", result["direction"])
        self.assertTrue(result["rule_debug"]["pet_visual_evidence"])

    def test_external_pet_calibration_requires_animal_visual_evidence(self) -> None:
        raw = evidence(
            visible_text=[
                "百万防癌险 保费额度600万 费用0.7元/月起",
                "具体保险方案及保费以实际投保时为准",
            ],
            visual=[
                "发光盾牌", "家庭亲子插画", "水彩风格插画", "草地",
                "树木", "蓝天", "父亲抱女儿", "母亲牵儿子",
            ],
            layout=["插画主导", "底部文字条"],
            action=["0.7元/月起"],
            scene=["户外家庭场景", "公园草地场景"],
        )
        raw["_image_name"] = "[保险服务]_family.jpg"

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("保险", result["business_line"])
        self.assertEqual("插画", result["direction"])
        self.assertFalse(result["rule_debug"]["pet_visual_evidence"])
        self.assertFalse(result["rule_debug"]["calibration_applied"])
        self.assertTrue(result["rule_debug"]["strong_illustration_visual_evidence"])
        self.assertIn("人物插画主体", result["rule_debug"]["calibration_rejected"])
        self.assertEqual(40, result["system_scores"]["direction"])

    def test_external_full_moon_visuals_outrank_icon_biased_action_text(self) -> None:
        raw = evidence(
            visible_text=[
                "赔付限额600万元",
                "赔付比例有社保60%起（无社保36%起）",
                "免赔额5万元及以内",
                "优享保障每月17元起",
            ],
            visual=[
                "大型金色圆环", "发光满月", "海洋波浪", "星空背景",
                "蓝色宝石", "金色星座连线", "光晕效果",
            ],
            layout=["中心圆环主体", "对称环绕布局", "底部文字说明", "宝石图标环绕"],
            action=[
                "优享保障每月17元起", "赔付限额600万元",
                "赔付比例60%起", "免赔额5万元及以内",
            ],
            scene=["夜晚海洋场景", "星空场景", "月光海面"],
        )
        raw["_image_name"] = "[保险服务]_full-moon.jpg"

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("保险", result["business_line"])
        self.assertEqual("月亮", result["direction"])
        self.assertEqual(40, result["system_scores"]["direction"])
        self.assertTrue(result["rule_debug"]["strong_moon_visual_evidence"])
        self.assertFalse(result["rule_debug"]["calibration_applied"])
        self.assertIn("强月亮视觉证据", result["rule_debug"]["calibration_rejected"])

    def test_external_text_led_benefit_list_outranks_auxiliary_icons(self) -> None:
        raw = evidence(
            visible_text=[
                "好医保·住院医疗(0免赔)体验版",
                "最高6000000元保额",
                "预约投保，前30天免费体验",
                "后续每月10.66元起",
                "0免赔额，责任内住院报销1元起赔",
            ],
            visual=[
                "绿色日历卡片", "白色盾牌图标", "绿色盾牌图标",
                "锁形图标", "人形图标", "绿色渐变背景",
            ],
            layout=["大号文字主导", "图标主导", "上下结构", "底部小字声明"],
            action=["预约投保", "免费体验", "每月10.66元起", "1元起赔"],
        )
        raw["_image_name"] = "[保险服务]_text-led-card.jpg"

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("保险", result["business_line"])
        self.assertEqual("上下左右图文", result["direction"])
        self.assertEqual(40, result["system_scores"]["direction"])
        self.assertFalse(result["rule_debug"]["text_led_poster_evidence"])
        self.assertTrue(result["rule_debug"]["balanced_split_evidence"])
        self.assertFalse(result["rule_debug"]["calibration_applied"])
        self.assertIn("均衡图文版式", result["rule_debug"]["calibration_rejected"])

    def test_external_large_center_icon_is_not_forced_to_poster(self) -> None:
        raw = evidence(
            visible_text=["百万医疗保障", "最高600万", "0免赔", "每月10元起"],
            visual=["大型盾牌", "盾牌主导", "锁形图标"],
            layout=["大号文字主导", "中心图标主导", "上下结构"],
            action=["立即投保", "最高600万", "0免赔", "每月10元起"],
        )
        raw["_image_name"] = "[保险服务]_large-center-icon.jpg"

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("图标", result["direction"])
        self.assertFalse(result["rule_debug"]["text_led_poster_evidence"])

    def test_external_centered_text_on_simple_gradient_is_poster(self) -> None:
        raw = evidence(
            visible_text=[
                "资金安心选", "账户安全险", "尊敬的客户您好",
                "您可凭手机号等个人信息免费领取账户安全险", "最高保额100万元",
            ],
            visual=["蓝色渐变背景", "斜线条纹理", "大号粗体文字", "浅黄色文字"],
            layout=["大号文字主导", "居中排版", "单屏全图广告"],
            action=["免费领取", "凭手机号等个人信息领取", "最高保额100万元"],
        )
        raw["_image_name"] = "[保险服务]_centered-text.jpg"

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("大字报", result["direction"])
        self.assertTrue(result["rule_debug"]["text_led_poster_evidence"])

    def test_external_dense_medical_benefit_text_is_poster(self) -> None:
        raw = evidence(
            visible_text=[
                "高血压II期人群带病可投保", "不含对应并发症", "赔付限额6000000元",
                "每月保费16.9元起", "等待期为30天", "责任内住院可申请报销",
                "最高报销比例100%", "具体以免责条款为准",
            ],
            visual=[
                "透明胶囊", "听诊器", "红色心形", "蓝色盾牌", "药片",
                "心电图线条", "黄色对勾圆形图标", "医疗主题插画",
            ],
            layout=["大号文字主导", "多区块图文", "图标主导"],
            action=["每月保费16.9元起"],
        )
        raw["_image_name"] = "[保险服务]_dense-benefits.jpg"

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("上下左右图文", result["direction"])
        self.assertFalse(result["rule_debug"]["text_led_poster_evidence"])
        self.assertTrue(result["rule_debug"]["balanced_split_evidence"])

    def test_v81_user_samples_split_text_and_graphics_are_not_icons(self) -> None:
        cases = (
            evidence(
                visible_text=["一键获取10+保司车险报价", "买哪家你决定", "免费查看报价"],
                visual=["大型盾牌图标", "汽车", "柱状图", "饼图", "数据图表"],
                layout=[
                    "单层画布", "文字第一视觉主体", "大号文字主导", "多区块图文",
                    "图标主导", "中心图标第一视觉主体",
                ],
            ),
            evidence(
                visible_text=["门诊买药也能赔", "责任内赔付总限额5万元", "立即投保", "每月保费28-106.5元"],
                visual=["3D文档图标", "橙色对勾图标", "3D展示台底座"],
                layout=[
                    "单层画布", "中心图标第一视觉主体", "左右分栏", "左侧文字区右侧图标区",
                    "大号标题主导", "多行权益列表", "按钮区", "图文并列版式",
                ],
            ),
            evidence(
                visible_text=["好医保·门诊险", "责任内赔付总限额50000元", "每月保费28-106.5元", "立即投保"],
                visual=["蓝色云朵图标", "白色向上箭头", "透明圆环"],
                layout=[
                    "单层画布", "左右分栏", "中心图标第一视觉主体", "大号文字主导", "信息卡片列表",
                ],
            ),
            evidence(
                visible_text=["好医保·门诊险", "赔付限额50000元", "每月保费28-106.5元", "立即投保"],
                visual=["蓝色漏斗", "蓝色胶囊", "柱状图文件", "数据节点"],
                layout=[
                    "单层画布", "中心图标第一视觉主体", "左右分栏", "多区块图文",
                    "大号文字主导", "图标主导",
                ],
            ),
        )
        names = (
            "[保险服务]_score81.3_tAO.jpg",
            "[保险服务]_score86.3_Fyy.jpg",
            "[保险服务]_score91.6_jV5.jpg",
            "[保险服务]_score87.3_ibm.jpg",
        )
        # 2026-09-29 新增「违规素材」拦截：jV5 这张的视觉证据里有「白色向上箭头」，
        # 按业务侧规则（userdata/violation_rules.json）归「违规素材」，不再落版式方向。
        # 本用例要测的是**版式证据**，所以下面照旧断言 balanced_split_evidence，
        # 只有方向期望按违规拦截调整。
        intercepted = {"[保险服务]_score91.6_jV5.jpg": "违规素材"}
        for raw, name in zip(cases, names):
            with self.subTest(name=name):
                raw["_image_name"] = name
                result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")
                self.assertEqual(
                    intercepted.get(name, "上下左右图文"), result["direction"]
                )
                self.assertTrue(result["rule_debug"]["balanced_split_evidence"])
                self.assertFalse(result["rule_debug"]["strong_icon_visual_evidence"])

    def test_v81_admuse_square_image_is_moments_before_visual_rules(self) -> None:
        raw = evidence(
            visible_text=["30岁交", "40岁领", "最高保额600万元"],
            visual=["大型盾牌", "家庭人物背影", "金币", "红色书本"],
            layout=["单层画布", "文字第一视觉主体", "中心图标第一视觉主体", "大号文字主导"],
        )
        raw.update({
            "_image_name": "[保险服务]_score73.1_X5s.jpg",
            "_image_width": 800,
            "_image_height": 800,
        })

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("朋友圈", result["direction"])
        self.assertTrue(result["rule_debug"]["square_image"])

    def test_v81_square_rule_is_limited_to_admuse(self) -> None:
        raw = evidence(
            visible_text=["保障相伴"],
            visual=["大型盾牌", "盾牌主导"],
            layout=["单层画布", "中心图标第一视觉主体"],
        )
        raw.update({"_image_width": 800, "_image_height": 800})

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="appgrowing")

        self.assertNotEqual("朋友圈", result["direction"])
        self.assertFalse(result["rule_debug"]["square_image"])

    def test_v81_only_text_on_pure_scenery_is_poster(self) -> None:
        cases = (
            evidence(
                visible_text=["最高保额600万元", "为爱保驾护航", "免赔额各5万元"],
                visual=["秋季树林仰拍", "多色树叶", "蓝天背景", "半透明文字框"],
                layout=["单层画布", "文字第一视觉主体", "全幅风景底图", "中心文字浮层", "大标题主导"],
                scene=["秋季树林", "自然风景", "户外仰拍"],
            ),
            evidence(
                visible_text=["美好生活", "保障相伴", "最高保额600万元"],
                visual=["森林", "高大树木", "蓝天", "阳光"],
                layout=["单层画布", "文字第一视觉主体", "竖版大字标题", "底部小字说明"],
                scene=["森林场景", "自然风景"],
            ),
        )
        for raw in cases:
            raw["_image_name"] = "[保险服务]_pure-scenery-poster.jpg"
            result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")
            self.assertEqual("大字报", result["direction"])
            self.assertTrue(result["rule_debug"]["pure_scenery_poster_evidence"])

    def test_v81_text_on_urban_scenery_is_poster(self) -> None:
        raw = evidence(
            visible_text=[
                "爱在保障中", "0免赔（限升级方案责任范围内）",
                "升级方案赔付比例100%", "累计保额300万",
            ],
            visual=[
                "城市天际线", "海湾", "船只", "红屋顶建筑",
                "水域", "高层建筑群", "云层",
            ],
            layout=[
                "单层画布", "文字第一视觉主体", "大标题居中",
                "文字覆盖风景", "底部免责声明",
            ],
            scene=["城市风光", "海滨风景", "城市天际线"],
        )
        raw.update({
            "graphic_type": "其他",
            "composition_relation": "文字主导",
            "composition_structure": "文字覆盖风景",
            "background_type": "其他",
            "text_overlay_on_scenery": True,
            "competing_visual_subject": False,
            "_image_name": "[保险服务]_urban-scenery-poster.jpg",
        })

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("大字报", result["direction"])
        self.assertTrue(result["rule_debug"]["pure_scenery_poster_evidence"])

    def test_v81_graphic_dominant_full_scenery_with_text_overlay_is_poster(self) -> None:
        raw = evidence(
            visible_text=[
                "赔付限额600万", "保费预估每月0.1元",
                "具体保险产品名称、承保保险公司、保险金额及费率以页面为准",
            ],
            visual=["海面", "太阳", "地平线", "天空渐变", "云层", "海面倒影"],
            layout=["单层画布", "第一视觉主体不明确", "全幅风景底图", "右下角小字免责声明"],
            scene=["海景", "自然风景"],
        )
        raw.update({
            "graphic_type": "风景照片",
            "composition_relation": "图案主导",
            "composition_structure": "文字覆盖风景",
            "background_type": "纯自然风景",
            "text_overlay_on_scenery": True,
            "competing_visual_subject": False,
            "_image_name": "[保险服务]_full-scenery-small-text.jpg",
        })

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("大字报", result["direction"])
        self.assertTrue(result["rule_debug"]["text_led_poster_evidence"])
        self.assertFalse(result["rule_debug"]["strong_icon_visual_evidence"])

    def test_v81_text_led_decorative_frames_are_posters(self) -> None:
        cases = (
            evidence(
                visible_text=["百万保险守护您的人生", "赔付额度600万", "每月保费0.7元起"],
                visual=[
                    "白色对话框", "绿色描边", "绿色叶子装饰", "红色爱心装饰",
                    "蓝色水滴装饰", "卡通插画边框", "米黄色背景",
                ],
                layout=["单层画布", "文字第一视觉主体", "大号文字主导"],
            ),
            evidence(
                visible_text=["百万保险守护您的人生", "赔付额度600万", "每月保费0.7元起"],
                visual=[
                    "橙色描边对话气泡", "红色爱心", "白色医疗十字", "橙色加号装饰",
                    "绿色热带叶子", "橙黄色抽象色块", "黄色十字星星",
                ],
                layout=[
                    "单层画布", "文字第一视觉主体", "中心文字气泡",
                    "大号文字主导", "四角装饰元素",
                ],
            ),
        )
        structured = (
            ("插画", "其他", False),
            ("其他", "抽象渐变", True),
        )
        for raw, (graphic_type, background_type, competing) in zip(cases, structured):
            raw.update({
                "graphic_type": graphic_type,
                "composition_relation": "文字主导",
                "composition_structure": "中心单主体",
                "background_type": background_type,
                "text_overlay_on_scenery": False,
                "competing_visual_subject": competing,
                "_image_name": "[保险服务]_decorative-text-frame.jpg",
            })
            with self.subTest(graphic_type=graphic_type, background_type=background_type):
                result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")
                self.assertEqual("大字报", result["direction"])
                self.assertTrue(result["rule_debug"]["text_led_poster_evidence"])

    def test_v81_calibration_prediction_is_audit_only(self) -> None:
        raw = evidence(
            visible_text=["保险保障", "每月保费0.7元起"],
            visual=[],
            layout=["单层画布"],
        )
        raw["_image_name"] = "[保险服务]_calibration-audit-only.jpg"
        candidates = [{"name": "图标", "score": -1.0}]

        with patch.object(
            classifier,
            "calibrated_direction_prediction",
            return_value=("图标", 3.0, 8, candidates),
        ):
            result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("未知方向", result["direction"])
        self.assertFalse(result["rule_debug"]["calibration_applied"])
        self.assertEqual(candidates, result["rule_debug"]["calibrated_direction_candidates"])
        self.assertIn("仅供审计", result["rule_debug"]["calibration_rejected"])

    def test_v81_split_illustration_and_text_is_balanced_without_calibration(self) -> None:
        raw = evidence(
            visible_text=[
                "电动车专属意外险", "投保成功享30天免费体验",
                "电动车起火能赔，充电器也能保",
            ],
            visual=["电动车起火插画", "火焰", "夜晚街道", "路灯"],
            layout=[
                "单层画布", "文字第一视觉主体", "上下图文",
                "大号文字主导", "顶部插画区块",
            ],
            scene=["夜晚街道场景", "电动车燃烧场景"],
        )
        raw.update({
            "graphic_type": "插画",
            "composition_relation": "文字主导",
            "composition_structure": "上下图文",
            "background_type": "其他",
            "text_overlay_on_scenery": False,
            "competing_visual_subject": True,
            "_image_name": "[保险服务]_split-scooter-ad.jpg",
        })

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("上下左右图文", result["direction"])
        self.assertTrue(result["rule_debug"]["balanced_split_evidence"])
        self.assertFalse(result["rule_debug"]["calibration_applied"])

    def test_v81_text_on_low_information_abstract_background_is_poster(self) -> None:
        raw = evidence(
            visible_text=["申请账户安全险", "最高保额100万元", "立即申请"],
            visual=[
                "白色票券卡片", "锯齿状卡片上边缘", "橙色渐变背景",
                "圆形装饰色块", "红色圆角按钮", "大号描边标题文字",
            ],
            layout=["单层画布", "文字第一视觉主体", "大号文字主导", "居中排版"],
            scene=["抽象装饰背景"],
        )
        raw.update({
            "graphic_type": "不明确",
            "composition_relation": "文字主导",
            "composition_structure": "中心单主体",
            "background_type": "抽象渐变",
            "text_overlay_on_scenery": False,
            "competing_visual_subject": False,
            "_image_name": "[保险服务]_abstract-orange-poster.jpg",
        })

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("大字报", result["direction"])
        self.assertTrue(result["rule_debug"]["text_led_poster_evidence"])
        self.assertFalse(result["rule_debug"]["calibration_applied"])

    def test_v81_urban_background_with_competing_subject_is_not_poster(self) -> None:
        raw = evidence(
            visible_text=["城市家庭保障", "立即投保"],
            visual=["城市天际线", "家庭人物照片", "汽车产品"],
            layout=["文字第一视觉主体", "文字覆盖风景"],
            scene=["城市风光", "家庭场景"],
        )
        raw.update({
            "graphic_type": "人物照片",
            "composition_relation": "文字主导",
            "composition_structure": "文字覆盖风景",
            "background_type": "城市风景",
            "text_overlay_on_scenery": True,
            "competing_visual_subject": True,
            "_image_name": "[保险服务]_urban-family-ad.jpg",
        })

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertNotEqual("大字报", result["direction"])
        self.assertFalse(result["rule_debug"]["pure_scenery_poster_evidence"])

    def test_v81_structured_fields_separate_graphic_type_from_layout(self) -> None:
        raw = evidence(
            visible_text=["门诊买药也能赔", "赔付限额5万元", "每月保费28元起"],
            visual=["3D剪贴板图标", "橙色对勾图标"],
            layout=["单层画布", "左右分栏"],
        )
        raw.update({
            "graphic_type": "图标",
            "composition_relation": "图文均衡",
            "composition_structure": "左右图文",
            "background_type": "抽象渐变",
            "text_overlay_on_scenery": False,
            "competing_visual_subject": True,
            "_image_name": "[保险服务]_structured-balanced.jpg",
        })

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("上下左右图文", result["direction"])
        self.assertNotEqual("图标", result["direction"])

    def test_v81_sparse_single_subject_remains_icon(self) -> None:
        raw = evidence(
            visible_text=["安心保障"],
            visual=["单一蓝色盾牌图标", "大面积浅色留白"],
            layout=["单层画布", "中心图标第一视觉主体", "单一主体", "大面积留白"],
        )
        raw.update({
            "graphic_type": "图标",
            "composition_relation": "图案主导",
            "composition_structure": "中心单主体",
            "background_type": "抽象渐变",
            "text_overlay_on_scenery": False,
            "competing_visual_subject": True,
            "_image_name": "[保险服务]_sparse-icon.jpg",
        })

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("图标", result["direction"])
        self.assertTrue(result["rule_debug"]["strong_icon_visual_evidence"])

    def test_v81_disabled_center_multi_element_marker_cannot_force_split_direction(self) -> None:
        raw = evidence(
            visible_text=["百万医疗险", "投保条件0~70岁"],
            visual=["满月", "月相变化", "同心圆轨道", "金色光晕", "星空背景"],
            layout=["单层画布", "月亮球体第一视觉主体", "中心主体+多元素信息场景"],
            scene=["星空场景"],
        )
        raw["_image_name"] = "[有米云]_disabled-center-multi-element.jpg"

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="appgrowing")

        self.assertEqual("月亮", result["direction"])
        self.assertNotIn("中心主体+多元素信息场景", result["keywords"]["layout_keywords"])
        self.assertNotIn("中心主体+多元素信息场景", result["reason"])

    def test_v81_center_single_icons_override_incorrect_balanced_api_field(self) -> None:
        cases = (
            evidence(
                visible_text=[
                    "岁月不饶人 是时候考虑你的保险了 聪明的人这样做",
                    "百万医疗险 总保额600万 有垫付服务",
                    "图片内容属广告创意，具体费率及保险金额以实际投保为准",
                ],
                visual=["渐变灯泡", "银色挂锁", "3D立体图标", "发光射线"],
                layout=["单层画布", "中心图标第一视觉主体", "上下图文"],
                action=["总保额600万", "有垫付服务"],
            ),
            evidence(
                visible_text=[
                    "图片内容属广告创意，具体费率及保险金额以实际投保为准",
                    "保险产品详情请见投保须知及保险条款",
                    "岁月不饶人 是时候考虑你的保险了 聪明的人这样做",
                    "百万医疗险 每月保费0.6元起 升级版重疾0免赔",
                ],
                visual=["蓝色挂锁图标", "白色对勾圆形图标", "锁形安全符号"],
                layout=["单层画布", "中心图标第一视觉主体", "图标主导", "上下图文"],
                action=["每月保费0.6元起", "升级版重疾0免赔"],
            ),
        )
        names = (
            "[保险服务]_score53.4_OFHpNp0.jpg",
            "[保险服务]_score53.3_BJnnqZ3.jpg",
        )
        for raw, name in zip(cases, names):
            with self.subTest(name=name):
                raw.update({
                    "graphic_type": "图标",
                    "composition_relation": "图文均衡",
                    "composition_structure": "上下图文" if "53.4" in name else "中心单主体",
                    "background_type": "其他",
                    "text_overlay_on_scenery": False,
                    "competing_visual_subject": True,
                    "_image_name": name,
                })
                result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")
                self.assertEqual("图标", result["direction"])
                self.assertTrue(result["rule_debug"]["strong_icon_visual_evidence"])
                self.assertFalse(result["rule_debug"]["balanced_split_evidence"])

    def test_v81_center_single_icon_priority_survives_benefit_panels(self) -> None:
        cases = (
            evidence(
                visible_text=["赔付限额600万元", "每月保费16.9元起", "0周岁-70周岁", "带病可投保"],
                visual=["蓝色盾牌图标", "点赞手势", "环绕小盾牌", "展示台"],
                layout=["单层画布", "中心图标第一视觉主体", "图标主导", "多区块图文"],
            ),
            evidence(
                visible_text=["最高保额600万", "保费每月16.9元起", "带病可投"],
                visual=["金色皇冠图标", "圆形徽章", "金色圆环"],
                layout=["单层画布", "中心图标第一视觉主体", "上下图文"],
            ),
            evidence(
                visible_text=["百万惠享医疗险", "赔付限额600万", "次月保费16.9元起", "可带病投保"],
                visual=["黄色手机图标", "闪电符号", "齿轮图标"],
                layout=["单层画布", "中心图标第一视觉主体", "上下图文", "底部信息卡片"],
            ),
        )
        structures = ("中心单主体", "中心单主体", "上下图文")
        names = (
            "[保险服务]_score81.1_Fqob.jpg",
            "[保险服务]_score78.2_xwkq.jpg",
            "[保险服务]_score80.3_q2rY.jpg",
        )
        for raw, structure, name in zip(cases, structures, names):
            with self.subTest(name=name):
                raw.update({
                    "graphic_type": "图标",
                    "composition_relation": "图文均衡",
                    "composition_structure": structure,
                    "background_type": "其他",
                    "text_overlay_on_scenery": False,
                    "competing_visual_subject": True,
                    "_image_name": name,
                })
                result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")
                self.assertEqual("图标", result["direction"])
                self.assertTrue(result["rule_debug"]["center_single_icon_evidence"])
                self.assertFalse(result["rule_debug"]["balanced_split_evidence"])

    def test_v81_explicit_moon_subject_outranks_center_single_icon_fields(self) -> None:
        raw = evidence(
            visible_text=["累计保额300万", "升级方案赔付比例100%", "每月0.1元起"],
            visual=["发光月亮", "钻石图标", "同心圆光环", "极光渐变背景", "中心球体"],
            layout=["单层画布", "月亮球体第一视觉主体", "中心单主体", "底部文字信息区"],
            scene=["夜空场景", "极光背景"],
        )
        raw.update({
            "graphic_type": "图标",
            "composition_relation": "图案主导",
            "composition_structure": "中心单主体",
            "background_type": "其他",
            "text_overlay_on_scenery": False,
            "competing_visual_subject": True,
            "_image_name": "[保险服务]_score85.2_tWjOb.jpg",
        })

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("月亮", result["direction"])
        self.assertTrue(result["rule_debug"]["strong_moon_visual_evidence"])
        self.assertFalse(result["rule_debug"]["center_single_icon_evidence"])

    def test_internal_full_moon_visuals_outrank_icon_filename_hint(self) -> None:
        raw = evidence(
            visible_text=[
                "百万医疗险",
                "免赔额、赔付比例、费率、保障责任等请见保险条款及投保须知",
                "岁月不饶人是时候考虑你的保险了,百万医疗险保障为你护航",
            ],
            visual=[
                "发光蓝色满月", "清晰月面纹理", "月球外同心圆环",
                "环绕圆形靶心按钮图标", "八个白色圆点圆圈", "夜空",
                "湖面倒影", "岸边树木剪影",
            ],
            layout=[
                "单层画布", "月亮球体第一视觉主体", "中心构图",
                "顶部说明文字条", "底部宣传文字条",
            ],
            scene=["夜晚湖景", "夜景自然风光", "水面倒影场景"],
        )
        raw.update({
            "graphic_type": "月亮/天体",
            "composition_relation": "图案主导",
            "composition_structure": "中心单主体",
            "background_type": "纯自然风景",
            "text_overlay_on_scenery": True,
            "competing_visual_subject": True,
            "_image_name": "075_20260728-百万医疗险-闪屏-盾牌-图标-纯AI-示例组-示例用户-文字-25--93-1097.jpg",
        })

        for platform in ("internal", "admuse", "appgrowing", "reyun"):
            with self.subTest(platform=platform):
                result = classifier.classify_keyword_evidence(raw, 0.85, platform=platform)
                self.assertEqual("月亮", result["direction"])
                self.assertTrue(result["rule_debug"]["strong_moon_visual_evidence"])

    def test_medical_moon_grid_template_outranks_center_diamond_icon(self) -> None:
        raw = evidence(
            visible_text=[
                "百万医疗险", "赔付限额600万元,免赔额5万元及以内",
                "保费预估每月0.6元起(有社保),赔付比例24%起(有社保)",
            ],
            visual=[
                "发光钻石", "透明气泡球体", "环绕圆形节点按钮",
                "夜空", "雪山", "湖面", "水面倒影",
            ],
            layout=["单层画布", "中心图标第一视觉主体", "底部文字信息区"],
            scene=["夜晚山水场景", "湖泊水面", "山脉夜景"],
        )
        raw.update({
            "graphic_type": "图标",
            "composition_relation": "图案主导",
            "composition_structure": "中心单主体",
            "background_type": "纯自然风景",
            "text_overlay_on_scenery": True,
            "competing_visual_subject": True,
            "_image_name": "20260430-百万医疗险-闪屏-月亮-GP组-倪民-头条-纯AI-25.jpg",
        })

        for platform in ("internal", "admuse", "appgrowing", "reyun"):
            with self.subTest(platform=platform):
                result = classifier.classify_keyword_evidence(raw, 0.85, platform=platform)
                self.assertEqual("月亮", result["direction"])
                self.assertTrue(result["rule_debug"]["medical_moon_grid_hint"])

    def test_v81_center_sphere_with_orbital_rings_is_celestial_not_icon(self) -> None:
        raw = evidence(
            visible_text=["大病小病均可保障", "免赔额以保险条款为准"],
            visual=[
                "黑色背景", "金色圆环", "中央黑色球体", "同心圆纹理",
                "黑胶唱片式圆盘", "金色中心孔", "3D金属质感",
            ],
            layout=["单层画布", "中心图标第一视觉主体", "同心圆构图", "底部小字条"],
        )
        raw.update({
            "graphic_type": "其他",
            "composition_relation": "图案主导",
            "composition_structure": "中心单主体",
            "background_type": "其他",
            "text_overlay_on_scenery": False,
            "competing_visual_subject": True,
            "_image_name": "[保险服务]_black-gold-celestial-sphere.jpg",
        })

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("月亮", result["direction"])
        self.assertTrue(result["rule_debug"]["strong_moon_visual_evidence"])
        self.assertFalse(result["rule_debug"]["strong_icon_visual_evidence"])

    def test_v81_decorative_crescent_background_does_not_override_multiblock_text(self) -> None:
        raw = evidence(
            visible_text=[
                "孩子容易感冒发烧，门诊买药加起来好多钱",
                "大病住院、小病门诊都可保",
                "0天~17周岁的孩子都能申请投保",
            ],
            visual=[
                "深蓝星空夜景背景", "微信聊天气泡", "智能手机服务页截图",
                "小孩捧发光星星背影插画", "3D透明盾牌带十字", "弯月与星星装饰",
            ],
            layout=[
                "单层画布", "文字第一视觉主体", "多区块图文",
                "聊天对话式排版", "底部手机插画盾牌并列",
            ],
            scene=["微信聊天咨询场景", "夜晚星空场景"],
        )
        raw.update({
            "graphic_type": "其他",
            "composition_relation": "文字主导",
            "composition_structure": "多区块图文",
            "background_type": "插画场景",
            "text_overlay_on_scenery": False,
            "competing_visual_subject": True,
            "_image_name": "[保险服务]_decorative-crescent-chat.jpg",
        })

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("上下左右图文", result["direction"])
        self.assertFalse(result["rule_debug"]["strong_moon_visual_evidence"])
        self.assertTrue(result["rule_debug"]["balanced_split_evidence"])

    def test_v81_retro_machine_carrying_complete_insurance_ad_is_derived(self) -> None:
        raw = evidence(
            visible_text=[
                "守护健康·医疗险", "最高保额600万", "保费每月0.1元",
                "审核通过，输入手机号等信息立即激活",
            ],
            visual=[
                "绿色白色胶囊", "复古米色机器", "红色球形摇杆",
                "红色纹理墙面", "螺旋电话线", "红色挂牌面板",
            ],
            layout=["单层画布", "中心单主体", "机器设备承载广告文字", "上下多面板结构"],
            scene=["红色墙面装饰背景", "复古设备展示场景"],
        )
        raw.update({
            "graphic_type": "其他",
            "composition_relation": "图案主导",
            "composition_structure": "中心单主体",
            "background_type": "其他",
            "text_overlay_on_scenery": False,
            "competing_visual_subject": True,
            "_image_name": "[保险服务]_retro-machine-insurance-ad.jpg",
        })

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("秒思自动衍生", result["direction"])
        self.assertTrue(result["rule_debug"]["derived_nested_hint"])
        self.assertFalse(result["rule_debug"]["strong_icon_visual_evidence"])

    def test_external_shooting_monitor_with_inner_ad_is_derived(self) -> None:
        raw = evidence(
            visible_text=[
                "输入手机号认证即可领取爱尔保险", "以爱为盾，保障亲友岁岁无忧",
                "赔付限额300万", "保险产品详见投保须知及保险条款",
            ],
            visual=[
                "摄像机", "监视器屏幕", "锁图标", "圆形进度环",
                "绿色圆形按钮", "设备指示灯",
            ],
            layout=["图标主导", "大号文字主导", "上下结构", "品牌Logo置顶"],
            action=["输入手机号认证", "领取保险"],
            scene=["影视拍摄场景", "设备操作场景", "夜间灯光场景"],
        )
        raw["_image_name"] = "[保险服务]_shooting-monitor.jpg"

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("秒思自动衍生", result["direction"])
        self.assertTrue(result["rule_debug"]["derived_nested_hint"])
        self.assertTrue(result["rule_debug"]["shooting_monitor_derived_hint"])

    def test_external_amusement_park_couple_is_illustration_not_moon(self) -> None:
        raw = evidence(
            visible_text=["守护健康·百万医疗险", "最高保额600万元"],
            visual=[
                "卡通情侣插画", "过山车车厢", "摩天轮", "游乐园夜景",
                "星空背景", "灯光装饰", "信息卡片", "复选框图标",
            ],
            layout=["场景插画在上", "信息卡片在下", "图标加文字列表"],
            action=["每月0.1元起", "最高保额600万元"],
            scene=["游乐园场景", "夜晚场景", "情侣约会场景"],
        )
        raw["_image_name"] = "[保险服务]_couple-night.jpg"

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("插画", result["direction"])
        self.assertTrue(result["rule_debug"]["strong_illustration_visual_evidence"])

    def test_external_family_moon_festival_is_illustration_not_moon(self) -> None:
        raw = evidence(
            visible_text=["买好保险，守护您的健康", "百万防癌险"],
            visual=[
                "发光月亮", "桂花枝", "月饼", "茶壶", "卡通插画",
                "家庭三代人物", "老人人物", "儿童人物",
            ],
            layout=["图文结合", "对话气泡", "底部大字标题"],
            action=["0.7元/月起"],
            scene=["中秋赏月场景", "庭院场景", "家庭团聚场景"],
        )
        raw["_image_name"] = "[保险服务]_family-moon-festival.jpg"

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("插画", result["direction"])
        self.assertTrue(result["rule_debug"]["strong_illustration_visual_evidence"])
        self.assertTrue(result["rule_debug"]["strong_moon_visual_evidence"])

    def test_external_person_photo_vetoes_incorrect_illustration_layout_marker(self) -> None:
        raw = evidence(
            visible_text=["一张保单守护三代人", "全家保·百万医疗险", "立即投保"],
            visual=[
                "家庭人物照片", "三代同堂", "白发老年女性", "中年男性",
                "中年女性", "儿童", "白发老年男性", "户外庭院",
            ],
            layout=[
                "单层画布", "插画场景第一视觉主体", "左右图文",
                "图标列表", "主标题大字号", "橙色按钮",
            ],
            scene=["家庭场景", "户外庭院场景", "生活方式场景"],
        )
        raw.update({
            "graphic_type": "人物照片",
            "composition_relation": "图文均衡",
            "composition_structure": "左右图文",
            "background_type": "带人物风景",
            "text_overlay_on_scenery": False,
            "competing_visual_subject": True,
            "_image_name": "[保险服务]_person-photo.jpg",
        })

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("上下左右图文", result["direction"])
        self.assertFalse(result["rule_debug"]["strong_illustration_visual_evidence"])
        self.assertTrue(result["rule_debug"]["balanced_split_evidence"])

    def test_external_drawn_medium_keeps_illustration_subject_marker(self) -> None:
        raw = evidence(
            visible_text=["为自己和家人增加保障", "赔付限额600万元"],
            visual=["卡通插画", "家庭亲子插画", "男性人物", "女性人物", "儿童人物"],
            layout=["单层画布", "插画场景第一视觉主体", "上下图文", "多区块图文"],
            scene=["家庭场景", "游乐园场景", "花海场景"],
        )
        raw.update({
            "graphic_type": "插画",
            "composition_relation": "图文均衡",
            "composition_structure": "上下图文",
            "background_type": "插画场景",
            "text_overlay_on_scenery": False,
            "competing_visual_subject": True,
            "_image_name": "[保险服务]_drawn-family.jpg",
        })

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("插画", result["direction"])
        self.assertTrue(result["rule_debug"]["strong_illustration_visual_evidence"])

    def test_sparse_icon_and_illustration_conflict_is_icon_on_all_platforms(self) -> None:
        raw = evidence(
            visible_text=["百万医疗险", "每月保费0.6元起"],
            visual=["卡通插画", "图标", "大面积浅色留白"],
            layout=[
                "单层画布", "插画场景第一视觉主体", "图标主导",
                "中心单主体", "大面积留白",
            ],
        )
        raw.update({
            "graphic_type": "插画",
            "composition_relation": "图案主导",
            "composition_structure": "中心单主体",
            "background_type": "纯色/简单抽象",
            "text_overlay_on_scenery": False,
            "competing_visual_subject": True,
            "_image_name": "[保险服务]_score51.2_sparse-icon-illustration.jpg",
        })

        for platform in ("internal", "admuse", "appgrowing", "reyun"):
            with self.subTest(platform=platform):
                result = classifier.classify_keyword_evidence(
                    copy.deepcopy(raw), 0.85, platform=platform
                )

                self.assertEqual("图标", result["direction"])
                self.assertFalse(result["rule_debug"]["strong_illustration_visual_evidence"])
                self.assertTrue(result["rule_debug"]["explicit_icon_keyword_evidence"])
                self.assertTrue(result["rule_debug"]["large_negative_space_evidence"])
                self.assertTrue(result["rule_debug"]["sparse_icon_illustration_conflict"])
                self.assertIn("大面积留白", result["reason"])

    def test_external_center_cube_with_corner_icons_is_icon_not_moon(self) -> None:
        raw = evidence(
            visible_text=["护住家中柴米生计", "投保条件0~60岁"],
            visual=[
                "蓝色钻石图标", "绿色立方体", "蓝色立方体", "透明立方体",
                "中心发光立方体", "环形平台", "科技感底座", "对勾符号",
            ],
            layout=["中心主体构图", "四角图标分布", "底部信息栏", "多区块图文"],
            action=[],
            scene=["科技抽象场景"],
        )
        raw["_image_name"] = "[保险服务]_center-cube-icons.jpg"

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("图标", result["direction"])
        self.assertTrue(result["rule_debug"]["strong_icon_visual_evidence"])

    def test_external_large_lock_with_magnifiers_is_icon_not_moon(self) -> None:
        raw = evidence(
            visible_text=["0免赔", "累计保额300万", "每月0.1元起"],
            visual=[
                "黄色挂锁", "白色锁梁", "蓝色环形轨道", "蓝色小球",
                "白色圆形平台", "蓝色放大镜", "黄色放大镜手柄",
            ],
            layout=["图标主导", "中心主体", "底部文字说明", "边框装饰"],
            action=["0免赔", "100%赔付比例", "累计保额300万"],
        )
        raw["_image_name"] = "[保险服务]_large-lock.jpg"

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("图标", result["direction"])
        self.assertTrue(result["rule_debug"]["strong_icon_visual_evidence"])

    def test_embedded_words_without_outer_composite_do_not_force_derived(self) -> None:
        raw = evidence(
            layout=["完整原素材嵌入", "嵌套广告截图", "大号文字主导"],
            visual=["银色轿车", "城市道路", "底部横幅"],
            visible_text=["车险在线理赔", "保险产品详见保险条款"],
        )
        raw["_image_name"] = "[保险服务]_car.jpg"

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertNotEqual("秒思自动衍生", result["direction"])
        self.assertFalse(result["rule_debug"]["derived_nested_hint"])

    def test_complete_inner_ad_with_outer_template_is_derived(self) -> None:
        raw = evidence(
            layout=["完整原素材嵌入", "嵌套广告截图", "二次组合模板"],
            visual=["复古游戏机造型", "外层新增营销元素"],
            visible_text=["保险产品详见保险条款"],
        )
        raw["_image_name"] = "[保险服务]_derived.jpg"

        result = classifier.classify_keyword_evidence(raw, 0.85, platform="admuse")

        self.assertEqual("秒思自动衍生", result["direction"])
        self.assertTrue(result["rule_debug"]["derived_nested_hint"])

    def test_external_insurance_cards_with_outer_decoration_are_derived(self) -> None:
        cases = (
            evidence(
                layout=["复古游戏机造型", "屏幕+挂牌组合", "大号文字主导"],
                visual=["白色锁图标", "粉色圆形背景", "螺旋线条装饰", "红色摇杆", "红色挂牌"],
                visible_text=["百万医疗险", "最高保额600万", "保费预估0.1元/月起"],
                scene=["红色墙面背景", "复古街机场景"],
            ),
            evidence(
                layout=["卡片式界面", "图标主导", "大号文字主导"],
                visual=["卡通手", "星星装饰", "白色卡片", "紫色渐变背景"],
                visible_text=["赔付限额600万", "免赔额1万元", "认证保障"],
                scene=["卡通插画场景"],
            ),
            evidence(
                layout=["图标主导", "上下分栏", "大号文字主导", "多区块图文"],
                visual=["蓝色锁形图标", "同心圆光环", "六边形网格背景", "白色卡片"],
                visible_text=["癌症也可保", "累计保额300万", "投保门槛低"],
                scene=["保险宣传页面", "浅蓝色渐变背景"],
            ),
        )
        for raw in cases:
            with self.subTest(layout=raw["layout_keywords"]):
                result = classifier.classify_keyword_evidence(raw, 0.85, "appgrowing")
                self.assertEqual("秒思自动衍生", result["direction"])
                self.assertTrue(result["rule_debug"]["derived_nested_hint"])

    def test_prompt_requests_structured_embedded_ad_checks(self) -> None:
        self.assertIn("canvas_layer", classifier.PROMPT)
        self.assertIn("embedded_ad_complete", classifier.PROMPT)
        self.assertIn("embedded_media_panel", classifier.PROMPT)
        self.assertIn("outer_relayout", classifier.PROMPT)
        self.assertIn("上方完整保险广告+下方外层二次排版", classifier.PROMPT)
        old_custom_prompt = classifier.PROMPT.replace("embedded_ad_complete", "legacy_embedded_field")
        upgraded = classifier.effective_prompt({"api_classify_prompt": old_custom_prompt})
        self.assertIn("返回 JSON 顶层必须始终包含以下四个字段", upgraded)
        self.assertIn('"embedded_ad_complete":true或false', upgraded)
        self.assertIn('"embedded_media_panel":true或false', upgraded)
        self.assertIn("完整保险广告被做成独立票券", classifier.PROMPT)

    def test_youmi_upper_complete_ad_with_outer_copy_outranks_inner_lock(self) -> None:
        raw = evidence(
            visible_text=[
                "俊豪保险", "俊豪保险-限时优先投", "用了都说好", "正规保障",
                "保额高且保费低", "身份还未认证，立即点击确认保障",
                "癌症也可保 累计保额300万", "升级方案赔付比例100%（有社保）",
                "0免赔（限升级方案责任范围内）",
                "基础方案预计每月0.1元起，升级方案预计每月21.4元起",
                "保险产品详见投保须知及保险条款，具体费率及保险金额以实际投保为准",
            ],
            brand=["俊豪保险", "俊豪保险六边形Logo"],
            visual=[
                "蓝色挂锁图标", "蓝色同心圆", "3D立体锁", "白色圆角卡片",
                "蓝色标签按钮", "几何立方体线条", "蓝白渐变背景",
            ],
            layout=[
                "单层画布", "中心图标第一视觉主体", "中心单主体",
                "上方图标卡片下方文字", "多行短文案",
            ],
            action=[
                "立即点击确认保障", "限时优先投", "预计每月0.1元起",
                "预计每月21.4元起", "累计保额300万", "0免赔",
            ],
            scene=["抽象装饰背景"],
        )
        raw.update({
            "graphic_type": "图标",
            "composition_relation": "图案主导",
            "composition_structure": "中心单主体",
            "background_type": "抽象渐变",
            "text_overlay_on_scenery": False,
            "competing_visual_subject": True,
            "_image_name": "[有米云]_20260804_0413_sample.jpeg.jpg",
        })

        result = classifier.classify_keyword_evidence(raw, 0.85, "appgrowing")

        self.assertEqual("秒思自动衍生", result["direction"])
        self.assertTrue(result["rule_debug"]["derived_nested_hint"])
        self.assertFalse(result["rule_debug"]["strong_icon_visual_evidence"])

    def test_structured_double_layer_fields_force_derived(self) -> None:
        raw = evidence(
            layout=["单层画布", "中心图标第一视觉主体", "中心单主体"],
            visual=["蓝色挂锁图标", "白色圆角卡片"],
        )
        raw.update({
            "graphic_type": "图标",
            "composition_relation": "图案主导",
            "composition_structure": "中心单主体",
            "background_type": "抽象渐变",
            "text_overlay_on_scenery": False,
            "competing_visual_subject": True,
            "canvas_layer": "外层画布+内层完整广告",
            "embedded_ad_complete": True,
            "outer_relayout": True,
            "_image_name": "[保险服务]_structured-derived.jpg",
        })

        result = classifier.classify_keyword_evidence(raw, 0.85, "admuse")

        self.assertEqual("秒思自动衍生", result["direction"])
        self.assertEqual("外层画布+内层完整广告", result["keywords"]["canvas_layer"])
        self.assertTrue(result["keywords"]["embedded_ad_complete"])
        self.assertTrue(result["keywords"]["outer_relayout"])

    def test_dense_standalone_icon_card_without_outer_relayout_stays_icon(self) -> None:
        raw = evidence(
            visible_text=[
                "百万医疗险", "累计保额300万", "0免赔", "赔付比例100%",
                "每月0.1元起", "保险产品详见投保须知及保险条款",
            ],
            visual=["蓝色挂锁图标", "白色圆角卡片", "蓝白渐变背景"],
            layout=["单层画布", "中心图标第一视觉主体", "中心单主体"],
            action=["累计保额300万", "每月0.1元起"],
        )
        raw.update({
            "graphic_type": "图标",
            "composition_relation": "图案主导",
            "composition_structure": "中心单主体",
            "background_type": "抽象渐变",
            "text_overlay_on_scenery": False,
            "competing_visual_subject": True,
            "canvas_layer": "单层画布",
            "embedded_ad_complete": False,
            "outer_relayout": False,
            "_image_name": "[保险服务]_standalone-lock.jpg",
        })

        result = classifier.classify_keyword_evidence(raw, 0.85, "appgrowing")

        self.assertEqual("图标", result["direction"])
        self.assertFalse(result["rule_debug"]["derived_nested_hint"])

    def test_ticketized_complete_insurance_ad_is_derived_on_all_shared_platforms(self) -> None:
        raw = evidence(
            visible_text=[
                "★★★★★",
                "赔付限额600万元，赔付比例有社保60%起（无社保36%起）",
                "免赔额5万元及以内，优享保障每月17元起（依据费率表）",
                "具体保险产品名称、承保保险公司、保险金额及费率以页面为准",
                "产品为短期保险产品，具体权益详见说明页或保险条款",
                "别让病房开销压垮家，这份保险得有",
            ],
            visual=[
                "金属齿轮图标", "金色五星评分", "白色圆角卡片",
                "票券锯齿底边", "底部模糊条形码/条纹元素", "灰色渐变外底",
            ],
            layout=[
                "单层画布", "中心图标第一视觉主体", "上下排布", "大号标语横带",
            ],
            action=["每月17元起", "赔付限额600万元", "免赔额5万元及以内"],
        )
        raw.update({
            "graphic_type": "图标",
            "composition_relation": "图案主导",
            "composition_structure": "中心单主体",
            "background_type": "抽象渐变",
            "text_overlay_on_scenery": False,
            "competing_visual_subject": True,
            "canvas_layer": "单层画布",
            "embedded_ad_complete": False,
            "outer_relayout": False,
            "_image_name": "[有米云]_20260804_0016_sample.jpeg.jpg",
        })

        for platform in ("admuse", "appgrowing", "reyun"):
            with self.subTest(platform=platform):
                result = classifier.classify_keyword_evidence(copy.deepcopy(raw), 0.85, platform)
                self.assertEqual("秒思自动衍生", result["direction"])
                self.assertTrue(result["rule_debug"]["derived_nested_hint"])
                self.assertTrue(result["rule_debug"]["ticket_derived_hint"])
                self.assertFalse(result["rule_debug"]["strong_icon_visual_evidence"])
                self.assertIn("完整保险票券", result["reason"])

    def test_sparse_ticket_decoration_without_complete_ad_stays_icon(self) -> None:
        raw = evidence(
            visible_text=["百万医疗险", "每月17元起"],
            visual=[
                "金属齿轮图标", "白色圆角卡片", "票券锯齿底边",
                "底部条形码", "灰色渐变外底",
            ],
            layout=["单层画布", "中心图标第一视觉主体", "中心单主体"],
            action=["每月17元起"],
        )
        raw.update({
            "graphic_type": "图标",
            "composition_relation": "图案主导",
            "composition_structure": "中心单主体",
            "background_type": "抽象渐变",
            "text_overlay_on_scenery": False,
            "competing_visual_subject": True,
            "canvas_layer": "单层画布",
            "embedded_ad_complete": False,
            "outer_relayout": False,
            "_image_name": "[保险服务]_sparse-ticket-icon.jpg",
        })

        result = classifier.classify_keyword_evidence(raw, 0.85, "appgrowing")

        self.assertEqual("图标", result["direction"])
        self.assertFalse(result["rule_debug"]["derived_nested_hint"])
        self.assertFalse(result["rule_debug"]["ticket_derived_hint"])

    def test_youmi_embedded_media_card_with_outer_brand_rating_is_derived(self) -> None:
        raw = evidence(
            visible_text=[
                "岁月不饶人",
                "考虑你的保险了，聪明的人这样做",
                "天天有保",
                "天天有保百万医疗险",
                "生活处处是风险！保障延续千万别忽视！",
            ],
            brand=["天天有保", "天天有保Logo", "绿叶太阳Logo"],
            visual=[
                "粉色菱形徽章", "粉色放射纹路", "蓝色圆形底座", "五颗黄色星星",
                "蓝色渐变上部背景", "白色立方体网格下部背景", "圆角白色卡片",
            ],
            layout=[
                "单层画布", "中心图标第一视觉主体", "上下图文", "圆角卡片", "上大图下大字",
            ],
        )
        raw.update({
            "graphic_type": "图标",
            "composition_relation": "图文均衡",
            "composition_structure": "上下图文",
            "background_type": "抽象渐变",
            "text_overlay_on_scenery": False,
            "competing_visual_subject": True,
            "canvas_layer": "单层画布",
            "embedded_ad_complete": False,
            "outer_relayout": False,
            "confidence": 0.72,
            "_image_name": "[有米云]_20260806_0057_sample.jpeg.jpg",
        })

        for platform in ("internal", "admuse", "appgrowing", "reyun"):
            with self.subTest(platform=platform):
                result = classifier.classify_keyword_evidence(copy.deepcopy(raw), 0.85, platform)
                self.assertEqual("秒思自动衍生", result["direction"])
                self.assertTrue(result["rule_debug"]["derived_nested_hint"])
                self.assertFalse(result["rule_debug"].get("strong_icon_visual_evidence", False))

    def test_structured_embedded_media_panel_forces_derived(self) -> None:
        raw = evidence(
            visible_text=["素材卡内标题", "天天有保", "百万医疗险", "外层宣传口号"],
            brand=["天天有保"],
            visual=["圆角白色卡片", "卡内独立主视觉"],
            layout=["外层画布+内层素材卡", "素材卡嵌入", "二次组合模板"],
        )
        raw.update({
            "graphic_type": "图标",
            "composition_relation": "图文均衡",
            "composition_structure": "上下图文",
            "background_type": "抽象渐变",
            "text_overlay_on_scenery": False,
            "competing_visual_subject": True,
            "canvas_layer": "外层画布+内层素材卡",
            "embedded_ad_complete": False,
            "embedded_media_panel": True,
            "outer_relayout": True,
            "_image_name": "[保险服务]_embedded-media-panel.jpg",
        })

        result = classifier.classify_keyword_evidence(raw, 0.85, "appgrowing")

        self.assertEqual("秒思自动衍生", result["direction"])
        self.assertEqual("外层画布+内层素材卡", result["keywords"]["canvas_layer"])
        self.assertTrue(result["keywords"]["embedded_media_panel"])

    def test_plain_pet_photo_card_without_outer_relayout_stays_pet(self) -> None:
        raw = evidence(
            visible_text=[
                "本图片由AI生成", "具体详见销售页面", "妈妈给我买个宠物险吧", "众安宠物险",
            ],
            brand=["众安宠物险"],
            visual=["银灰色虎斑小猫", "宠物猫咪", "圆角白色卡片"],
            layout=["单层画布", "宠物第一视觉主体", "上下图文", "上方图片下方文字"],
            action=["9.9元每月", "5万赔付总限额"],
            scene=["室内场景"],
        )
        raw.update({
            "graphic_type": "其他",
            "composition_relation": "图文均衡",
            "composition_structure": "上下图文",
            "background_type": "室内场景",
            "text_overlay_on_scenery": False,
            "competing_visual_subject": True,
            "canvas_layer": "单层画布",
            "embedded_ad_complete": False,
            "embedded_media_panel": False,
            "outer_relayout": False,
            "_image_name": "[有米云]_20260806_0044_sample.jpeg.jpg",
        })

        result = classifier.classify_keyword_evidence(raw, 0.85, "appgrowing")

        self.assertEqual("宠物", result["direction"])
        self.assertFalse(result["rule_debug"]["derived_nested_hint"])


if __name__ == "__main__":
    unittest.main()
