"""违规素材拦截规则。

三轮需求：
- 2026-09-29：保险的「图标」素材里，画面出现箭头的要单独落到「违规素材」，
  不再按图标归档；而且规则要能改，不能写死在代码里。
- 2026-10-10：模仿系统界面交互的图（播放按钮、关闭按钮等）也算违规素材。
- 2026-10-10：国旗国徽等涉政国家象征、涉黄低俗内容也算违规素材；插画类尤其要注意。

规则放在 userdata/violation_rules.json，这里覆盖：加载、归一化、匹配、
两条产出路径都要生效、include_uncertain 开关，以及后两轮需求的「能拦 + 不误伤」。

⚠️ 代码测试只能验证「字段有值时能不能拦」。模型对涉政涉黄有回避倾向，
可能干脆不填这个字段 —— 那属于**提示词效果**问题，代码测不出来，
必须用真实图片跑小样本验证模型是否愿意输出。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import _tempdir  # noqa: F401  # 把临时目录挪出 %TEMP%（见 _tempdir.py）
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import classify_pure_api as classifier  # noqa: E402


def rules_file(rules, tmp):
    path = Path(tmp) / "violation_rules.json"
    path.write_text(
        json.dumps({"version": 1, "rules": rules}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


class ViolationRulesLoadingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        classifier.load_violation_rules(reload=True)

    def tearDown(self):
        classifier.load_violation_rules(reload=True)
        self._tmp.cleanup()

    def _load(self, rules):
        path = rules_file(rules, self.tmp)
        with patch.dict(
            os.environ,
            {classifier.VIOLATION_RULES_ENV: str(path)},
            clear=False,
        ):
            return classifier.load_violation_rules(reload=True)

    def test_missing_file_is_created_from_defaults(self):
        path = Path(self.tmp) / "sub" / "violation_rules.json"
        with patch.dict(
            os.environ, {classifier.VIOLATION_RULES_ENV: str(path)}, clear=False
        ):
            loaded = classifier.load_violation_rules(reload=True)
        self.assertTrue(path.is_file(), "规则文件不存在时应自动生成")
        # 规则条数会随业务增加（2026-10-10 起默认 2 条：保险-箭头 + 界面伪装），
        # 所以断言「至少有一条」并点名必须包含的那条，而不是写死数量。
        self.assertGreaterEqual(len(loaded["rules"]), 1)
        ids = {rule.get("id") for rule in loaded["rules"]}
        self.assertIn("insurance-arrow", ids)
        self.assertIn("ui-mimicry", ids)

    def test_default_rule_targets_insurance_arrows(self):
        rule = classifier.DEFAULT_VIOLATION_RULES["rules"][0]
        self.assertEqual(["保险"], rule["business_lines"])
        self.assertEqual(classifier.VIOLATION_DIRECTION, rule["direction"])
        self.assertIn("箭头", rule["keywords"])

    def test_default_includes_ui_mimicry_rule(self):
        rules = {rule.get("id"): rule for rule in classifier.DEFAULT_VIOLATION_RULES["rules"]}
        rule = rules["ui-mimicry"]
        self.assertTrue(rule["enabled"])
        # 空 business_lines = 所有业务线都拦
        self.assertEqual([], rule["business_lines"])
        self.assertEqual(classifier.VIOLATION_DIRECTION, rule["direction"])
        # 必须限定字段：这些词在正常素材里也会出现（装饰弧线、品牌 Logo、卖点文案）
        self.assertEqual([classifier.UI_MIMICRY_FIELD], rule["match_fields"])
        for value in classifier.UI_MIMICRY_VALUES:
            self.assertIn(value, rule["keywords"])

    def test_rule_without_direction_is_dropped(self):
        loaded = self._load([{"keywords": ["箭头"], "direction": ""}])
        self.assertEqual([], loaded["rules"])

    def test_rule_without_keywords_is_dropped(self):
        loaded = self._load([{"keywords": [], "direction": "违规素材"}])
        self.assertEqual([], loaded["rules"])

    def test_broken_json_falls_back_to_defaults(self):
        path = Path(self.tmp) / "bad.json"
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        with patch.dict(
            os.environ, {classifier.VIOLATION_RULES_ENV: str(path)}, clear=False
        ):
            loaded = classifier.load_violation_rules(reload=True)
        self.assertGreaterEqual(len(loaded["rules"]), 1)
        self.assertIn(
            "ui-mimicry", {rule.get("id") for rule in loaded["rules"]}
        )

    def test_empty_business_lines_means_all_lines(self):
        loaded = self._load(
            [{"direction": "违规素材", "keywords": ["箭头"], "business_lines": []}]
        )
        self.assertEqual([], loaded["rules"][0]["business_lines"])


class ViolationRuleMatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        classifier.load_violation_rules(reload=True)
        self._tmp.cleanup()

    def _match(self, business_line, corpus, uncertain="", rules=None):
        rules = rules or [
            {
                "id": "insurance-arrow",
                "name": "保险-箭头素材",
                "business_lines": ["保险"],
                "direction": "违规素材",
                "keywords": ["箭头"],
            }
        ]
        path = rules_file(rules, self.tmp)
        with patch.dict(
            os.environ, {classifier.VIOLATION_RULES_ENV: str(path)}, clear=False
        ):
            classifier.load_violation_rules(reload=True)
            return classifier.match_violation_rule(business_line, corpus, uncertain)

    def test_insurance_with_arrow_is_intercepted(self):
        hit = self._match("保险", "红色箭头|图标主导")
        self.assertIsNotNone(hit)
        self.assertEqual("违规素材", hit[0])
        self.assertIn("箭头", hit[1])

    def test_insurance_without_arrow_is_not_intercepted(self):
        self.assertIsNone(self._match("保险", "发光月亮|权益图标"))

    def test_other_business_lines_are_not_intercepted(self):
        self.assertIsNone(self._match("APP", "红色箭头|网赚"))
        self.assertIsNone(self._match("卡券", "红色箭头"))

    def test_keyword_matches_as_substring(self):
        # 业务侧只写「箭头」，要能覆盖「白色向上箭头」「三角箭头图标」等
        for corpus in ("白色向上箭头", "三角箭头图标", "橙色箭头按钮", "箭头"):
            with self.subTest(corpus=corpus):
                self.assertIsNotNone(self._match("保险", corpus))

    def test_disabled_rule_does_not_fire(self):
        rules = [
            {
                "direction": "违规素材",
                "keywords": ["箭头"],
                "business_lines": ["保险"],
                "enabled": False,
            }
        ]
        self.assertIsNone(self._match("保险", "红色箭头", rules=rules))

    def test_first_matching_rule_wins(self):
        rules = [
            {
                "name": "第一条",
                "direction": "违规素材",
                "keywords": ["箭头"],
                "business_lines": ["保险"],
            },
            {
                "name": "第二条",
                "direction": "另一个方向",
                "keywords": ["箭头"],
                "business_lines": ["保险"],
            },
        ]
        hit = self._match("保险", "红色箭头", rules=rules)
        self.assertEqual("违规素材", hit[0])
        self.assertIn("第一条", hit[1])

    def test_uncertain_is_ignored_by_default(self):
        # 实测有素材的箭头只出现在 uncertain（AI 自己写了「含义不明确」），
        # 默认不拦，避免误伤
        self.assertIsNone(self._match("保险", "图标主导", uncertain="小型箭头状图标"))

    def test_include_uncertain_opt_in(self):
        rules = [
            {
                "direction": "违规素材",
                "keywords": ["箭头"],
                "business_lines": ["保险"],
                "include_uncertain": True,
            }
        ]
        hit = self._match("保险", "图标主导", uncertain="小型箭头状图标", rules=rules)
        self.assertIsNotNone(hit)
        self.assertEqual("违规素材", hit[0])


class ViolationDirectionRegistrationTests(unittest.TestCase):
    def test_violation_direction_is_registered_for_insurance(self):
        # 登记在保险的白名单里，方便维护时一眼看到
        self.assertIn(classifier.VIOLATION_DIRECTION, classifier.BUSINESS_LINE_DIRECTIONS["保险"])

    def test_violation_direction_has_no_scoring_rules(self):
        # 它不该参与常规打分，只由违规拦截写入
        self.assertNotIn(classifier.VIOLATION_DIRECTION, classifier.EXTERNAL_DIRECTION_SOURCES)

    def test_violation_direction_is_not_in_the_app_list(self):
        # APP_DIRECTIONS 是业务侧定的「内容方向 + 版式方向」清单；
        # 违规素材是引擎的拦截落点（2026-10-10 起所有业务线都能落），不是业务方向，
        # 所以不进这份清单 —— 它由违规拦截在最后一步写入，不参与方向打分。
        self.assertNotIn(classifier.VIOLATION_DIRECTION, classifier.APP_DIRECTIONS)


class ViolationOverridesBothPathsTests(unittest.TestCase):
    """两条产出路径都要拦，否则同一素材会因为平台不同落到不同方向。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        path = rules_file(
            [
                {
                    "name": "保险-箭头素材",
                    "business_lines": ["保险"],
                    "direction": "违规素材",
                    "keywords": ["箭头"],
                }
            ],
            self.tmp,
        )
        self._env = patch.dict(
            os.environ, {classifier.VIOLATION_RULES_ENV: str(path)}, clear=False
        )
        self._env.start()
        classifier.load_violation_rules(reload=True)

    def tearDown(self):
        self._env.stop()
        classifier.load_violation_rules(reload=True)
        self._tmp.cleanup()

    def _evidence(self):
        return {
            "visible_text": ["百万医疗险", "600万医疗保障"],
            "visual_keywords": ["红色箭头", "盾牌"],
            "layout_keywords": ["中心图标主导"],
            "graphic_type": "图标",
            "composition_relation": "图案主导",
            "composition_structure": "中心单主体",
            "confidence": 0.95,
            "uncertain": [],
        }

    def test_business_line_path_intercepts(self):
        result = classifier.classify_keyword_evidence(self._evidence(), 0.85, "admuse")
        self.assertEqual("违规素材", result["direction"])
        self.assertIn("箭头", str(result.get("reason")))

    def test_taxonomy_path_intercepts(self):
        result = classifier.apply_collection_taxonomy(
            classifier.classify_keyword_evidence(self._evidence(), 0.85, "internal")
        )
        self.assertEqual("违规素材", result["direction"])
        self.assertIn("违规拦截", str(result.get("reason")))

    def test_interception_marks_classification_complete(self):
        # 命中即确定违规，不该再算「仅业务线 / 产品」
        result = classifier.apply_collection_taxonomy(
            classifier.classify_keyword_evidence(self._evidence(), 0.85, "internal")
        )
        self.assertFalse(result.get("partial"))
        self.assertEqual("complete", result.get("classification_level"))

    def test_without_arrow_nothing_changes(self):
        evidence = self._evidence()
        evidence["visual_keywords"] = ["发光月亮", "盾牌"]
        result = classifier.classify_keyword_evidence(evidence, 0.85, "admuse")
        self.assertNotEqual("违规素材", result["direction"])


class UiMimicryRuleTests(unittest.TestCase):
    """界面伪装（业务侧 2026-10-10）：模仿系统/App 界面控件的图算违规素材。

    守两件事：
    1. **能拦住** —— 两条产出路径（内部系统 / 外部平台）都要落「违规素材」。
       外部平台路径一度漏传 field_corpora，表现为「内部系统拦得住、妙思拦不住」，
       所以单独钉一条测试。
    2. **不误伤** —— 「进度条」「播放按钮」这些词在正常素材里也会出现
       （装饰弧线、品牌 Logo、商品卖点文案），规则用 match_fields 限定字段才安全。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        # 用默认规则写一份到临时目录：既测了默认规则内容，又不耦合本机业务配置
        path = Path(self.tmp) / "violation_rules.json"
        path.write_text(
            json.dumps(classifier.DEFAULT_VIOLATION_RULES, ensure_ascii=False),
            encoding="utf-8",
        )
        self._env = patch.dict(
            os.environ, {classifier.VIOLATION_RULES_ENV: str(path)}, clear=False
        )
        self._env.start()
        classifier.load_violation_rules(reload=True)

    def tearDown(self):
        self._env.stop()
        classifier.load_violation_rules(reload=True)
        self._tmp.cleanup()

    @staticmethod
    def _evidence(**overrides):
        base = {
            "visible_text": [],
            "brand_keywords": [],
            "visual_keywords": ["广告素材"],
            "layout_keywords": ["单层画布"],
            "action_keywords": [],
            "scene_keywords": [],
            "graphic_type": "其他",
            "composition_relation": "不明确",
            "composition_structure": "其他",
            "background_type": "其他",
            "text_overlay_on_scenery": False,
            "competing_visual_subject": False,
            "canvas_layer": "单层画布",
            "embedded_ad_complete": False,
            "embedded_media_panel": False,
            "outer_relayout": False,
            "kuaishou_logo_variant": "不确定",
            "kuaishou_logo_style": "不确定",
            "confidence": 0.96,
            "uncertain": [],
            "_image_name": "x-百万医疗险-大图-1.jpg",
        }
        base.update(overrides)
        return base

    def _internal(self, evidence):
        return classifier.apply_collection_taxonomy(
            classifier.classify_keyword_evidence(evidence, 0.85, "internal")
        )

    def test_mimicry_elements_route_to_violation(self):
        for value in ("播放按钮", "关闭按钮", "全屏按钮", "静音按钮", "系统弹窗", "假搜索框"):
            with self.subTest(value=value):
                result = self._internal(
                    self._evidence(visible_text=["百万医疗险"],
                                   ui_mimicry_elements=[value])
                )
                self.assertEqual(classifier.VIOLATION_DIRECTION, result["direction"])
                self.assertEqual("complete", result["classification_level"])

    def test_mimicry_applies_to_every_business_line(self):
        cases = [
            ("保险", self._evidence(visible_text=["百万医疗险"],
                                    ui_mimicry_elements=["播放按钮"])),
            ("APP", self._evidence(visible_text=["快手"], brand_keywords=["橙色相机Logo"],
                                   ui_mimicry_elements=["关闭按钮"],
                                   _image_name="x-快手-短剧-1.jpg")),
            ("卡券", self._evidence(visible_text=["QQ阅读会员"],
                                    ui_mimicry_elements=["系统弹窗"],
                                    _image_name="x-QQ阅读-大图-1.jpg")),
        ]
        for label, evidence in cases:
            with self.subTest(business_line=label):
                self.assertEqual(
                    classifier.VIOLATION_DIRECTION, self._internal(evidence)["direction"]
                )

    def test_external_platform_path_also_blocks(self):
        """外部平台走另一条调用链，漏传 field_corpora 会静默失效。"""
        result = classifier.classify_keyword_evidence(
            self._evidence(visible_text=["百万医疗险"], ui_mimicry_elements=["播放按钮"]),
            0.85,
            "admuse",
        )
        self.assertEqual(classifier.VIOLATION_DIRECTION, result["direction"])

    def test_words_outside_the_field_do_not_trigger(self):
        """防误伤：词在别的字段、但模型没把它判成伪装控件时必须放过。

        下列样本全部取自真实素材：装饰弧线、品牌 Logo、权益列表图标、商品卖点文案。
        """
        samples = [
            ("装饰弧线", self._evidence(visible_text=["百万医疗险"],
                                        visual_keywords=["绿色弧形进度条"])),
            ("短剧品牌 Logo", self._evidence(visual_keywords=["播放按钮图标"],
                                              brand_keywords=["橙色圆角方块播放三角Logo"],
                                              _image_name="x-红果短剧-短剧-1.jpg")),
            ("月亮素材装饰圆点", self._evidence(layout_keywords=["八个圆点按钮"],
                                                visual_keywords=["月面纹理"],
                                                visible_text=["百万医疗险"])),
            ("商品卖点文案", self._evidence(visible_text=["静音滑轮"],
                                            visual_keywords=["行李箱"])),
            ("广告自身行动按钮", self._evidence(visible_text=["立即购买"],
                                                visual_keywords=["立即查看按钮", "购买按钮"])),
            ("普通圆形按钮", self._evidence(visual_keywords=["蓝色圆形按钮"])),
        ]
        for label, evidence in samples:
            with self.subTest(case=label):
                self.assertNotEqual(
                    classifier.VIOLATION_DIRECTION, self._internal(evidence)["direction"]
                )

    def test_cached_evidence_without_field_is_unaffected(self):
        """旧缓存没有 ui_mimicry_elements → 规则不命中，行为与改动前一致。"""
        evidence = self._evidence(
            visual_keywords=["绿色弧形进度条", "播放按钮图标", "八个圆点按钮"],
            brand_keywords=["橙色相机Logo"],
        )
        self.assertNotIn(classifier.UI_MIMICRY_FIELD, evidence)
        self.assertNotEqual(
            classifier.VIOLATION_DIRECTION, self._internal(evidence)["direction"]
        )

    def test_field_limited_rule_never_falls_back_to_whole_corpus(self):
        """match_fields 规则在字段为空时**不适用**，绝不能退化成扫全语料。

        这条是防误伤的最后一道闸：即便关键词命中其它字段，也不能触发。
        """
        hit = classifier.match_violation_rule(
            "保险",
            corpus="绿色弧形进度条|播放按钮图标|八个圆点按钮",   # 全语料里有这些词
            uncertain_corpus="",
            field_corpora={},                                   # 但限定字段为空
        )
        self.assertIsNone(hit)

    def test_legacy_rule_without_match_fields_still_scans_corpus(self):
        """旧的「保险-箭头」规则没有 match_fields，必须继续扫全语料。"""
        result = self._internal(
            self._evidence(visible_text=["百万医疗险"], visual_keywords=["右箭头按钮"])
        )
        self.assertEqual(classifier.VIOLATION_DIRECTION, result["direction"])

    def test_all_call_sites_pass_field_corpora(self):
        """源码级守护：match_violation_rule 的每个调用点都要传 field_corpora。

        漏传时规则静默失效、不报错 —— 这个坑已经在外部平台路径上踩过一次。
        """
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "classify_pure_api.py"
        ).read_text(encoding="utf-8")
        lines = source.splitlines()
        blocks = [
            "\n".join(lines[index:index + 8])
            for index, line in enumerate(lines)
            if "match_violation_rule(" in line and not line.strip().startswith("#")
        ]
        self.assertGreaterEqual(len(blocks), 2, "至少两处调用（内部系统 / 外部平台）")
        for block in blocks:
            if "def match_violation_rule" in block:
                continue
            with self.subTest(call=block.splitlines()[0].strip()):
                self.assertIn(
                    "field_corpora=", block,
                    "调用 match_violation_rule 必须传 field_corpora，"
                    "否则 match_fields 规则会静默失效",
                )


class ProhibitedContentTests(unittest.TestCase):
    """违禁元素（业务侧 2026-10-10）：国旗国徽等涉政象征、涉黄内容算违规素材。

    业务侧特别强调「插画类尤其要注意」—— 卡通/Q版风格最容易被整体忽略，
    该要求在 PROHIBITED_POLICY_ADDENDUM 里写明；这里从代码侧钉住
    「字段有值就必须落违规素材，不管是什么画风」。

    ⚠️ 代码测试无法验证「模型是否愿意输出这些值」。模型对涉政涉黄有回避倾向，
    可能干脆不填、导致规则空转 —— 那要靠小样本实跑验证（见下方说明）。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        path = Path(self.tmp) / "violation_rules.json"
        path.write_text(
            json.dumps(classifier.DEFAULT_VIOLATION_RULES, ensure_ascii=False),
            encoding="utf-8",
        )
        self._env = patch.dict(
            os.environ, {classifier.VIOLATION_RULES_ENV: str(path)}, clear=False
        )
        self._env.start()
        classifier.load_violation_rules(reload=True)

    def tearDown(self):
        self._env.stop()
        classifier.load_violation_rules(reload=True)
        self._tmp.cleanup()

    _evidence = staticmethod(UiMimicryRuleTests._evidence)

    def _internal(self, evidence):
        return classifier.apply_collection_taxonomy(
            classifier.classify_keyword_evidence(evidence, 0.85, "internal")
        )

    def test_political_symbols_route_to_violation(self):
        for value in ("国旗", "国徽", "党徽", "国家领导人形象", "人民币图案"):
            with self.subTest(value=value):
                result = self._internal(
                    self._evidence(visible_text=["百万医疗险"],
                                   prohibited_elements=[value])
                )
                self.assertEqual(classifier.VIOLATION_DIRECTION, result["direction"])
                self.assertEqual("complete", result["classification_level"])

    def test_adult_content_routes_to_violation(self):
        for value in ("色情裸露", "性暗示", "低俗挑逗"):
            with self.subTest(value=value):
                result = self._internal(
                    self._evidence(prohibited_elements=[value])
                )
                self.assertEqual(classifier.VIOLATION_DIRECTION, result["direction"])

    def test_illustration_style_is_treated_the_same(self):
        """插画/卡通风格不能例外 —— 业务侧点名「插画类尤其要注意」。"""
        for graphic in ("插画", "图标", "人物照片", "其他"):
            with self.subTest(graphic_type=graphic):
                result = self._internal(
                    self._evidence(visible_text=["百万医疗险"],
                                   graphic_type=graphic,
                                   prohibited_elements=["国旗"])
                )
                self.assertEqual(classifier.VIOLATION_DIRECTION, result["direction"])

    def test_applies_to_every_business_line_and_both_paths(self):
        evidence = self._evidence(visible_text=["快手"], brand_keywords=["橙色相机Logo"],
                                  prohibited_elements=["国旗"],
                                  _image_name="x-快手-短剧-1.jpg")
        self.assertEqual(classifier.VIOLATION_DIRECTION, self._internal(evidence)["direction"])
        external = classifier.classify_keyword_evidence(evidence, 0.85, "admuse")
        self.assertEqual(classifier.VIOLATION_DIRECTION, external["direction"])

    def test_words_outside_the_field_do_not_trigger(self):
        """防误伤：词出现在别的字段、但模型没判定为违禁元素时必须放过。

        「国」字在正常素材里极常见（国漫、国民好物），品牌标识也常被误认。
        """
        samples = [
            ("国漫文案", self._evidence(visible_text=["25元/月，热门国漫随心看"])),
            ("国风场景", self._evidence(scene_keywords=["古风玄幻场景"],
                                        visual_keywords=["古典楼阁"])),
            ("品牌角标", self._evidence(visible_text=["淘宝"],
                                        brand_keywords=["橙色红底白字淘宝角标"])),
            ("正常着装", self._evidence(visual_keywords=["女性黄色连体泳衣"],
                                        scene_keywords=["泳池边"])),
            ("连衣裙模特", self._evidence(visual_keywords=["写实人物照片", "宝蓝色连衣裙"],
                                          _image_name="x-淘宝联盟-衣服美女类-1.jpg")),
            ("人民币符号图标", self._evidence(visual_keywords=["人民币符号图标"])),
        ]
        for label, evidence in samples:
            with self.subTest(case=label):
                self.assertNotEqual(
                    classifier.VIOLATION_DIRECTION, self._internal(evidence)["direction"]
                )

    def test_cached_evidence_without_field_is_unaffected(self):
        evidence = self._evidence(visible_text=["25元/月，热门国漫随心看"])
        self.assertNotIn(classifier.PROHIBITED_FIELD, evidence)
        self.assertNotEqual(
            classifier.VIOLATION_DIRECTION, self._internal(evidence)["direction"]
        )

    def test_both_violation_fields_coexist(self):
        """界面伪装与违禁元素可以同时命中，都落「违规素材」。"""
        result = self._internal(
            self._evidence(visible_text=["百万医疗险"],
                           ui_mimicry_elements=["播放按钮"],
                           prohibited_elements=["国旗"])
        )
        self.assertEqual(classifier.VIOLATION_DIRECTION, result["direction"])

    def test_every_match_field_is_registered(self):
        """源码级守护：规则用到的 match_fields 必须登记进 VIOLATION_FIELD_NAMES。

        漏登记时 violation_field_corpora() 不会生成该字段的语料 → 规则静默失效。
        check_rules 的 C7 也会查，这里再加一道，避免绕过工具直接跑批。
        """
        rules = classifier.load_violation_rules().get("rules") or []
        for rule in rules:
            for field in rule.get("match_fields") or []:
                with self.subTest(rule=rule.get("id"), field=field):
                    self.assertIn(
                        field, classifier.VIOLATION_FIELD_NAMES,
                        f"字段「{field}」需要在 VIOLATION_FIELD_NAMES 里登记，"
                        f"否则规则会静默失效",
                    )


if __name__ == "__main__":
    unittest.main()
