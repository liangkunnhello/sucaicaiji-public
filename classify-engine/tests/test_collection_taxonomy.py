from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import classify_pure_api as classifier  # noqa: E402


def result(direction: str, *, product: str = "百万医疗险", keywords=None):
    return {
        "product": product,
        "specific_product": product,
        "business_line": "保险" if product == "百万医疗险" else "APP",
        "output_group": product,
        "direction": direction,
        "keywords": keywords or {},
        "rule_debug": {"image_name": "sample.jpg"},
        "reason": "原分类规则命中",
        "evidence": ["原规则证据"],
        "abstain": False,
        "partial": False,
        "classification_level": "complete",
    }


def raw_evidence(*, visible_text=None, brand_keywords=None, logo_variant="不确定",
                 logo_style="不确定", image_name="", layout_keywords=None,
                 composition_structure="其他"):
    return {
        "visible_text": visible_text or [],
        "brand_keywords": brand_keywords or [],
        "visual_keywords": ["广告素材"],
        "layout_keywords": layout_keywords or ["单层画布"],
        "action_keywords": [],
        "scene_keywords": [],
        "graphic_type": "其他",
        "composition_relation": "不明确",
        "composition_structure": composition_structure,
        "background_type": "其他",
        "text_overlay_on_scenery": False,
        "competing_visual_subject": False,
        "canvas_layer": "单层画布",
        "embedded_ad_complete": False,
        "embedded_media_panel": False,
        "outer_relayout": False,
        "kuaishou_logo_variant": logo_variant,
        "kuaishou_logo_style": logo_style,
        "confidence": 0.96,
        "uncertain": [],
        "_image_name": image_name,
    }


class CollectionTaxonomyTest(unittest.TestCase):
    def test_every_configured_product_has_unknown_and_derived_directions(self) -> None:
        products = classifier.COLLECTION_TAXONOMY["products"]

        # 产品数不写死：分类表由 tools/sync_taxonomy.py 从策略中心自动生成
        # （2026-09-22 起），数量会随策略中心目录变化。这里断言「策略中心产品数 + 本地例外登记数」，
        # 既防分类表被误改，又不用每次同步都回来改测试。
        overrides = json.loads(
            Path(classifier.__file__).with_name("taxonomy_overrides.json").read_text(
                encoding="utf-8")
        )
        source = classifier.COLLECTION_TAXONOMY.get("source") or {}
        self.assertTrue(source, "分类表缺少 source 段——请用 tools/sync_taxonomy.py 重新生成")
        expected = int(source.get("products") or 0) + len(overrides.get("extra_products") or {})
        self.assertEqual(expected, len(products))

        for product, spec in products.items():
            with self.subTest(product=product):
                self.assertIn("未知", spec["directions"])
                self.assertIn("妙思衍生", spec["directions"])

    def test_layout_direction_is_subdivided(self) -> None:
        """笼统的「上下左右图文」必须细分成分栏方向。

        实测两个内部批次共 238 条落在这个笼统方向里、其中 191 条被分类表门控成
        「未知」（分类表里没有这种版式词）。业务侧 2026-09-22 确认「你细分下吧」，
        按画面证据拆成 多区块／左右／上下 三种。
        """
        cases = [
            ("左右图文", ["左右分栏"], "左右图文"),
            ("上下图文", ["上下分栏"], "上下图文"),
            ("多区块图文", ["多区块图文"], "多区块图文"),
        ]
        for expected, layout, structure in cases:
            with self.subTest(expected=expected, layout=layout):
                classified = classifier.classify_keyword_evidence(
                    raw_evidence(
                        visible_text=["百万医疗险"],
                        layout_keywords=["单层画布", *layout],
                        composition_structure=structure,
                    ),
                    0.85,
                )
                self.assertEqual(expected, classified["direction"])

        # 多种分栏证据同时存在时由引擎打分定（左右赢了平局）；这里只保证一件事：
        # 结果必须落在细分名里，不能又是笼统的「上下左右图文」。
        mixed = classifier.classify_keyword_evidence(
            raw_evidence(
                visible_text=["百万医疗险"],
                layout_keywords=["单层画布", "左右分栏", "多区块图文"],
                composition_structure="多区块图文",
            ),
            0.85,
        )
        self.assertIn(mixed["direction"], classifier.LAYOUT_SUBDIRECTION_NAMES)

    def test_aliased_internal_product_maps_to_lingjing_name(self) -> None:
        """引擎内部叫「QQ阅读」，策略中心叫「QQ阅读-25元」——落盘目录必须跟策略中心一致。

        没有这条别名，卡券素材会被判成「分类表里没有 QQ阅读」→ 整条落 未知/未知。
        """
        products = classifier.COLLECTION_TAXONOMY["products"]
        self.assertIn("QQ阅读-25元", products)
        self.assertNotIn("QQ阅读", products)
        self.assertEqual("QQ阅读-25元",
                         classifier.COLLECTION_PRODUCT_ALIASES.get("QQ阅读"))

    def test_allowed_direction_uses_three_level_destination(self) -> None:
        classified = classifier.apply_collection_taxonomy(result("图标"))

        self.assertEqual("保险", classified["output_group"])
        self.assertEqual("百万医疗险", classified["output_product"])
        self.assertEqual("图标", classified["direction"])
        self.assertEqual(
            Path("output") / "保险" / "百万医疗险" / "图标",
            classifier.classification_destination(Path("output"), classified),
        )

    def test_out_of_table_direction_goes_to_product_unknown(self) -> None:
        classified = classifier.apply_collection_taxonomy(result("宠物"))

        self.assertEqual("宠物", classified["detected_direction"])
        self.assertEqual("未知", classified["direction"])
        self.assertTrue(classified["partial"])
        self.assertEqual(
            Path("output") / "保险" / "百万医疗险" / "未知",
            classifier.classification_destination(Path("output"), classified),
        )

    def test_missing_product_uses_business_line_default(self) -> None:
        source = result("图标")
        source.update({
            "product": "待确认",
            "specific_product": "",
            "business_line": "保险",
            "output_group": "保险",
        })

        classified = classifier.apply_collection_taxonomy(source)

        self.assertEqual("百万医疗险", classified["output_product"])
        self.assertEqual("图标", classified["direction"])

    def test_explicit_out_of_table_product_does_not_use_default(self) -> None:
        # 用「联通19元200G」当表外产品：它只存在于内部词表，不在对外分类表里。
        # （原用例用的是「百万防癌险」，2026-09-21 已补进分类表，不再适用。）
        source = result("图标", product="联通19元200G")
        source.update({"business_line": "保险", "output_group": "保险"})

        classified = classifier.apply_collection_taxonomy(source)

        self.assertEqual("联通19元200G", classified["detected_product"])
        self.assertEqual("未知", classified["output_group"])
        self.assertEqual("未知", classified["output_product"])
        self.assertEqual("未知", classified["direction"])
        self.assertEqual(
            Path("output") / "未知" / "未知" / "未知",
            classifier.classification_destination(Path("output"), classified),
        )

    def test_all_legacy_derived_names_share_one_direction(self) -> None:
        for direction in ("自动衍生", "妙思自动衍生", "秒思自动衍生"):
            with self.subTest(direction=direction):
                classified = classifier.apply_collection_taxonomy(result(direction))
                self.assertEqual("妙思衍生", classified["direction"])

    def test_split_layout_with_clear_poster_evidence_becomes_poster(self) -> None:
        keywords = {
            "visible_text": ["百万医疗险", "最高保额600万元", "每月0.1元起"],
            "brand_keywords": [],
            "visual_keywords": ["家庭人物照片"],
            "layout_keywords": ["单层画布", "大号标题", "权益列表", "上下图文"],
            "action_keywords": ["每月0.1元起"],
            "scene_keywords": ["家庭生活场景"],
            "graphic_type": "人物照片",
            "composition_relation": "图文均衡",
            "composition_structure": "上下图文",
            "canvas_layer": "单层画布",
        }

        classified = classifier.apply_collection_taxonomy(
            result("上下左右图文", keywords=keywords)
        )

        self.assertEqual("海报", classified["direction"])
        self.assertTrue(classified["rule_debug"]["taxonomy_poster_evidence"])

    def test_generic_split_layout_without_poster_evidence_goes_unknown(self) -> None:
        keywords = {
            "visible_text": ["百万医疗险"],
            "brand_keywords": [],
            "visual_keywords": ["信息图标"],
            "layout_keywords": ["左右分栏"],
            "action_keywords": [],
            "scene_keywords": [],
            "graphic_type": "图标",
            "composition_relation": "图文均衡",
            "composition_structure": "左右图文",
            "canvas_layer": "单层画布",
        }

        classified = classifier.apply_collection_taxonomy(
            result("上下左右图文", keywords=keywords)
        )

        self.assertEqual("未知", classified["direction"])
        self.assertFalse(classified["rule_debug"]["taxonomy_poster_evidence"])

    def test_baidu_netdisk_unsegmented_maps_to_regular(self) -> None:
        classified = classifier.apply_collection_taxonomy(
            result("未细分", product="百度网盘")
        )

        self.assertEqual("APP-拉新", classified["output_group"])
        # 2026-09-28 业务侧定：APP 方向收敛到固定清单，「常规」不在清单里 → 归内容创意
        self.assertEqual("内容创意", classified["direction"])

    def test_kuaishou_logo_without_lightning_is_regular_app(self) -> None:
        classified = classifier.classify_keyword_evidence(
            raw_evidence(
                brand_keywords=["橙黄色快手相机形Logo"],
                logo_variant="不带小闪电",
            ),
            0.85,
        )

        self.assertEqual("快手", classified["product"])
        self.assertEqual(
            "快手",
            classified["rule_debug"]["kuaishou_logo_product_override"],
        )

    def test_kuaishou_logo_with_lightning_is_lite_app(self) -> None:
        classified = classifier.classify_keyword_evidence(
            raw_evidence(
                brand_keywords=["橙黄色快手相机形Logo"],
                logo_variant="右下角带小闪电",
            ),
            0.85,
        )

        self.assertEqual("快手极速版", classified["product"])

    def test_kuaishou_uncertain_logo_does_not_default_to_regular_app(self) -> None:
        """2026-09-21 行为变更：图标看不清时不再默认判普通快手。

        旧实现「看到相机形 Logo 但小闪电未明确 → 判普通快手」在图标只有 30~70px 时
        会把快手极速版系统性地误判成快手（重放实测 8 条版本混淆里有 4 条源于此）。
        """
        without_name = classifier.classify_keyword_evidence(
            raw_evidence(
                brand_keywords=["左上角橙色相机形Logo"],
                logo_variant="不确定",
            ),
            0.85,
        )
        self.assertNotEqual("快手", without_name["product"])

        # 文件名已明确标注版本时，用它补齐
        with_name = classifier.classify_keyword_evidence(
            raw_evidence(
                brand_keywords=["左上角橙色相机形Logo"],
                logo_variant="不确定",
                image_name="100.17-20260907-快手极速版-老歌-林田子-纯AI-235-1080x1920.jpg",
            ),
            0.85,
        )
        self.assertEqual("快手极速版", with_name["product"])

    def test_kuaishou_visual_evidence_wins_over_filename(self) -> None:
        """「命名只能参考」：视觉证据与文件名冲突时，以视觉证据为准。

        2026-09-21 用户定调：文件名是弱标签，只在视觉证据沉默时兜底，
        不得在冲突时压倒它。
        """
        classified = classifier.classify_keyword_evidence(
            raw_evidence(
                brand_keywords=["橙色相机Logo", "白色小闪电"],
                logo_variant="右下角带小闪电",
                image_name="3638.67-18819-20250908-王钦翰-快手-闪屏-旅游攻略-9跑量.jpg",
            ),
            0.85,
        )
        # 视觉证据（小闪电）说极速版，文件名说快手 → 以视觉证据为准
        self.assertEqual("快手极速版", classified["product"])

    def test_kuaishou_filename_fills_in_only_when_evidence_silent(self) -> None:
        """颜色与小闪电都给不出结论时，才用文件名补齐版本（证据沉默兜底）。"""
        classified = classifier.classify_keyword_evidence(
            raw_evidence(
                brand_keywords=["橙色相机形Logo"],
                logo_variant="不确定",
                logo_style="不确定",
                image_name="100.17-20260907-快手极速版-老歌-林田子-纯AI-235-1080x1920.jpg",
            ),
            0.85,
        )
        self.assertEqual("快手极速版", classified["product"])

    def test_kuaishou_logo_color_decides_version(self) -> None:
        """V8.3：颜色（纯色/渐变）比小闪电可靠，同为相机形 Logo 时的判据。"""
        gradient = classifier.classify_keyword_evidence(
            raw_evidence(brand_keywords=["橙色相机Logo"], logo_style="橙色渐变"),
            0.85,
        )
        self.assertEqual("快手极速版", gradient["product"])

        flat = classifier.classify_keyword_evidence(
            raw_evidence(brand_keywords=["橙色相机Logo"], logo_style="纯橙色无渐变"),
            0.85,
        )
        self.assertEqual("快手", flat["product"])

    def test_kuaishou_camera_logo_lightning_still_has_lite_priority(self) -> None:
        classified = classifier.classify_keyword_evidence(
            raw_evidence(
                brand_keywords=["左上角橙色相机形Logo"],
                logo_variant="右下角带小闪电",
            ),
            0.85,
        )

        self.assertEqual("快手极速版", classified["product"])

    def test_generic_orange_button_and_lightning_do_not_imply_kuaishou(self) -> None:
        classified = classifier.classify_keyword_evidence(
            raw_evidence(
                brand_keywords=["橙色领取按钮", "画面右侧闪电装饰"],
                logo_variant="不确定",
            ),
            0.85,
        )

        self.assertEqual("待确认", classified["product"])

    def test_complete_lite_name_has_priority_when_logo_is_uncertain(self) -> None:
        classified = classifier.classify_keyword_evidence(
            raw_evidence(visible_text=["快手极速版"], logo_variant="不确定"),
            0.85,
        )

        self.assertEqual("快手极速版", classified["product"])

    def test_explicit_kuaishou_store_copy_confirms_product_without_logo(self) -> None:
        classified = classifier.classify_keyword_evidence(
            raw_evidence(
                visible_text=["有营业执照的老板们注意了", "在快手内即可开店"],
                brand_keywords=["快手"],
                logo_variant="未识别到橙黄色快手Logo",
            ),
            0.85,
            "admuse",
        )
        classified = classifier.apply_collection_taxonomy(classified)

        self.assertEqual("APP-拉新", classified["output_group"])
        self.assertEqual("快手", classified["output_product"])
        # 业务侧 2026-09-28 定：招商/电商经营类归「快手电商」（原来是「电商」，
        # 两个名字已合并），不能掉进「内容创意」。
        self.assertEqual("快手电商", classified["direction"])
        self.assertIn(
            "快手开店、商城或电商经营文案",
            classified["rule_debug"]["kuaishou_logo_evidence"],
        )

    def test_generic_kuaishou_text_does_not_force_version(self) -> None:
        classified = classifier.classify_keyword_evidence(
            raw_evidence(visible_text=["快手"], logo_variant="不确定"),
            0.85,
        )

        self.assertEqual("待确认", classified["product"])

    def test_invalid_logo_variant_is_normalized_to_uncertain(self) -> None:
        raw = raw_evidence(logo_variant="不明确")

        normalized = classifier.normalize_keyword_evidence(raw)

        self.assertEqual("不确定", normalized["kuaishou_logo_variant"])

    def test_app_semantic_rule_overrides_old_layout_direction(self) -> None:
        keywords = raw_evidence(
            visible_text=["快手商城", "商家入驻享流量扶持"],
            logo_variant="不带小闪电",
        )
        classified = classifier.apply_collection_taxonomy(
            result("上下左右图文", product="快手", keywords=keywords)
        )

        # 语义规则命中「快手电商」，且它现在在 APP 方向清单内，不再被收敛掉。
        self.assertEqual("快手电商", classified["direction"])
        self.assertTrue(
            classified["rule_debug"]["app_direction_calibration_applied"]
        )

    def test_photography_pose_guide_is_content_creative(self) -> None:
        keywords = raw_evidence(
            visible_text=["摄影", "摆拍攻略"],
            logo_variant="不带小闪电",
        )
        classified = classifier.apply_collection_taxonomy(
            result("网赚", product="快手", keywords=keywords)
        )

        self.assertEqual("内容创意", classified["direction"])
        self.assertIn(
            "摄影、摆拍或出片教程语义",
            classified["rule_debug"]["app_direction_calibration_reason"],
        )

    def test_generic_photography_does_not_force_content_creative(self) -> None:
        direction, reason = classifier.strong_app_direction(
            raw_evidence(visible_text=["摄影"], logo_variant="不带小闪电"),
            "快手",
        )

        self.assertIsNone(direction)
        self.assertEqual("", reason)

    def test_unknown_product_keeps_calibrated_direction(self) -> None:
        source = result(
            "上下左右图文",
            product="待确认",
            keywords=raw_evidence(visible_text=["全网低价好物", "9.9包邮"]),
        )
        source.update({
            "specific_product": "",
            "business_line": "APP",
            "platform": "admuse",
        })

        classified = classifier.apply_collection_taxonomy(source)

        self.assertEqual("未知", classified["output_group"])
        self.assertEqual("未知", classified["output_product"])
        # 业务侧 2026-09-22 明确：购物就是低价好物，策略中心用「购物」，所以套分类表时改名
        self.assertEqual("购物", classified["direction"])
        self.assertEqual(
            Path("output") / "未知" / "未知" / "购物",
            classifier.classification_destination(Path("output"), classified),
        )

    def test_ecommerce_direction_is_no_longer_downgraded(self) -> None:
        # 2026-09-28 业务侧定「电商」并入「快手电商」，原来的
        # 「产品未知时快手电商 → 电商」降级已取消：再降级会打到清单外（→ 内容创意）。
        self.assertEqual(
            "快手电商",
            classifier.normalize_app_direction_for_product("快手电商", "待确认"),
        )
        self.assertEqual(
            "快手电商",
            classifier.normalize_app_direction_for_product("快手电商", "快手"),
        )

    def test_app_calibration_model_contains_human_truth_only(self) -> None:
        model = classifier.APP_DIRECTION_CALIBRATION_MODEL

        self.assertEqual(233, model["sample_count"])
        self.assertGreaterEqual(len(model["tokens"]), 3000)
        serialized = str(model)
        self.assertNotIn("[电商平台]", serialized)
        self.assertNotIn("20260818-3", serialized)


if __name__ == "__main__":
    unittest.main()
