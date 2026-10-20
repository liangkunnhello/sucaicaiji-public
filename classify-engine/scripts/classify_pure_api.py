#!/usr/bin/env python3
"""
API 极速分类器。

仅调用视觉模型 API 提取可见关键词，不让模型直接决定分类。业务线、产品
和方向由本地确定性规则打分，并结合统计校准词表持续校准；通过
低置信度拒绝、并发、重试和结果缓存提高稳定性。
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import math
import os
import shutil
import sys
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from api_audit_excel import export_api_audit_xlsx


PROMPT_VERSION = "v8.2-app-logo-r2"

# 输出预算。deepseek-v4.1-flash 这类推理型模型会先消耗大量内部推理 token，
# 而推理内容不体现在 content 里；预算不够时正文一个字符都生成不出来，
# 表现为 HTTP 200 + content=[{"type":"text","text":""}] + stop_reason=max_tokens。
# 实测（861KB 大图 + 4657 字提示词）：实际需要 2000~4200 token，
# 3000 会间歇失败（实测 0/5），8000 稳定通过（5/5）。
# 推理型模型把内部推理也算进 max_tokens，预算被推理吃光时正文一个字都出不来。
# 实测 572 条的 output_tokens：中位 774、均值 1519，但有 67 条 >3000、最长一条 11215。
# 8000 仍会偶发截断并触发"预算翻倍重试"，白跑一次往返（大图场景单次就上百秒）。
# 提到 16000：max_tokens 只是上限，按实际生成量计费，调高不产生额外成本。
DEFAULT_MAX_TOKENS = 16000
MAX_TOKENS_CEILING = 32000

DIRECTION_CALIBRATION_PATH = Path(__file__).with_name("direction_calibration_v1.json")
APP_DIRECTION_CALIBRATION_PATH = Path(__file__).with_name(
    "collection_app_calibration_v1.json"
)
COLLECTION_TAXONOMY_PATH = Path(__file__).with_name("collection_taxonomy_v1.json")
try:
    DIRECTION_CALIBRATION_MODEL: dict[str, Any] = json.loads(
        DIRECTION_CALIBRATION_PATH.read_text(encoding="utf-8")
    )
except (OSError, ValueError, TypeError):
    DIRECTION_CALIBRATION_MODEL = {}
try:
    APP_DIRECTION_CALIBRATION_MODEL: dict[str, Any] = json.loads(
        APP_DIRECTION_CALIBRATION_PATH.read_text(encoding="utf-8")
    )
except (OSError, ValueError, TypeError):
    APP_DIRECTION_CALIBRATION_MODEL = {}
COLLECTION_TAXONOMY: dict[str, Any] = json.loads(
    COLLECTION_TAXONOMY_PATH.read_text(encoding="utf-8")
)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
KEYWORD_PROMPT_FIELDS = (
    "visible_text",
    "brand_keywords",
    "visual_keywords",
    "layout_keywords",
    "action_keywords",
    "scene_keywords",
    "confidence",
)
STRUCTURED_PROMPT_FIELDS = (
    "graphic_type",
    "composition_relation",
    "composition_structure",
    "background_type",
    "text_overlay_on_scenery",
    "competing_visual_subject",
)
OPTIONAL_STRUCTURED_PROMPT_FIELDS = (
    "canvas_layer",
    "embedded_ad_complete",
    "embedded_media_panel",
    "outer_relayout",
    "kuaishou_logo_variant",
    "kuaishou_logo_style",
    # 2026-10-10：模仿系统/App 界面控件的元素（违规素材拦截用）。
    # ⚠️ 必须留在 OPTIONAL 组：effective_prompt 的 compatible 检查只看
    # KEYWORD_PROMPT_FIELDS + STRUCTURED_PROMPT_FIELDS，而配置里的自定义提示词
    # （4657 字）不含这个新字段名——一旦挪进那两组，compatible 变 False，
    # 整个自定义提示词会被静默丢弃、回退到代码内 PROMPT。
    "ui_mimicry_elements",
    # 2026-10-10：违禁元素（涉政国家象征 / 涉黄低俗），违规素材拦截用。
    # 同样必须留在 OPTIONAL 组，理由见上面 ui_mimicry_elements 的注释。
    "prohibited_elements",
)
STRUCTURED_FIELD_CHOICES: dict[str, set[str]] = {
    "graphic_type": {"图标", "月亮/天体", "插画", "人物照片", "风景照片", "产品照片", "其他", "不明确"},
    "composition_relation": {"文字主导", "图案主导", "图文均衡", "不明确"},
    "composition_structure": {
        "左右图文", "上下图文", "中心单主体", "多区块图文", "文字覆盖风景", "其他", "不明确",
    },
    "background_type": {
        "纯自然风景", "城市风景", "纯色/简单抽象", "带人物风景", "插画场景", "抽象渐变",
        "室内场景", "其他", "不明确",
    },
    "canvas_layer": {
        "单层画布", "外层画布+内层完整广告", "外层画布+内层素材卡", "空间层级不确定",
    },
    "kuaishou_logo_variant": {
        "右下角带小闪电",
        "不带小闪电",
        "未识别到橙黄色快手Logo",
        "不确定",
    },
    # 2026-09-21 新增：快手与快手极速版的图标都很小（常见 30~70px），
    # 右下角的小闪电在缩略尺寸下经常看不出来，模型会两个方向都判错。
    # 实测颜色（纯色 vs 渐变）比小闪电可辨得多，故单列一个字段。
    "kuaishou_logo_style": {
        "纯橙色无渐变",
        "橙色渐变",
        "未识别到橙黄色快手Logo",
        "不确定",
    },
}
DISABLED_LAYOUT_MARKERS = {"中心主体+多元素信息场景"}

# 内部系统先按产品收窄方向；外部平台复用同一词表，按业务线比较方向。
PRODUCT_DIRECTIONS: dict[str, list[str]] = {
    "QQ阅读": [
        "插画", "盾牌", "图标", "小程序", "诱导类", "月亮", "自动衍生",
        # 2026-09-22 细分：原来笼统的「上下左右图文」拆成三种分栏方向（业务侧已确认）
        "多区块图文", "左右图文", "上下图文",
    ],
    "百度": ["未细分"],
    "百度网盘": ["未细分"],
    "百度极速版": ["网赚", "美女"],
    "百万防癌险": ["图标", "多区块图文", "左右图文", "上下图文", "大字报", "月亮"],
    "百万医疗险": [
        "插画", "大字报", "家庭亲子插画", "图标", "盾牌", "月亮",
        # 2026-09-22 细分：左右图文本身就是细分名，再补上另外两种分栏
        "左右图文", "多区块图文", "上下图文",
        # 策略中心对百万医疗险另有这两个方向，本地点漏了（不补就永远产不出）
        "内容创意", "综合卖点",
    ],
    # 业务侧要求单独成产品的示例：该产品与同业务线的宽泛产品（百万医疗险）
    # 证据高度重叠，方向需保持一致。靠 PRODUCT_RULES 里的正负分让它优先胜出，
    # 见下面的 negative 登记。
    "京东保": [
        "插画", "大字报", "家庭亲子插画", "图标", "盾牌", "月亮",
        "左右图文", "多区块图文", "上下图文",
        "内容创意", "综合卖点", "海报",
    ],
    "快手": [
        "老歌", "旅游", "旅游攻略", "美女", "美女网赚", "萌宠", "网赚",
        "文字大字报", "幸福生活", "一分购", "低价好物", "快手电商",
        "电商", "短剧", "美食", "内容创意", "官方样式", "猎奇",
    ],
    "快手极速版": [
        "旅游", "旅游攻略", "妙思自动衍生", "网赚", "文字大字报", "一分购",
        "低价好物", "快手电商", "电商", "短剧", "美食", "内容创意",
        "官方样式", "猎奇",
    ],
    "淘宝联盟": ["未细分"],
    "喜番免费短剧": ["短剧", "网赚"],
    "红果短剧": ["短剧", "漫剧", "网赚"],
    "中国移动APP-签到有礼+10元话费": [
        "签到有礼",
        # 策略中心把移动APP的方向拆成两条具体权益，照它补上（否则这两条永远产不出）
        "签到有礼-100元话费+2G流量", "签到有礼-10元话费+2G流量",
    ],
    "抖音商城": ["多区块图文", "左右图文", "上下图文", "0.01元", "0.1元", "一分购"],
    "小红书": ["美妆", "美食"],
    "联通19元200G": ["朋友圈"],
    # 2026-09-22 按策略中心补：这两个产品策略中心已建（闲鱼 1 个方向、今日头条 3 个方向），
    # 本地点没有，于是它们的素材只能落「未知/未知」。方向严格照策略中心写。
    "闲鱼": ["导买"],
    "今日头条": ["网赚", "资讯", "追剧"],
}
DIRECTION_ALIASES: dict[str, str] = {
    "家庭亲子插画": "插画",
    "盾牌": "图标",
    "盾牌锁": "图标",
    # 注意：这里**不能**再把「左右图文」并到「上下左右图文」——
    # 2026-09-22 起「左右图文」是细分后的正式方向名，并回去会导致永远细分不出来。
}


# 「上下左右图文」是一族版式，不是具体方向：它把「上方插画+下方权益」「左侧插画+
# 右侧文字块」「多区块信息图」全混在一个桶里。实测两个内部批次共 238 条落在这里，
# 其中 191 条被分类表门控成「未知」（分类表里没有这种笼统的版式词）。
# 按画面证据细分，业务侧 2026-09-22 确认「你细分下吧」。顺序即优先级：
# 多区块最具体，其次左右分栏，最后上下分栏。
LAYOUT_SUBDIRECTIONS: list[tuple[str, list[str]]] = [
    ("多区块图文", [
        "多区块图文", "上下左右分区", "四区块", "组合信息区块",
        "权益卡片列表", "信息卡片", "多行权益",
    ]),
    ("左右图文", [
        "左右分栏", "左图右文", "右文左图", "左文右图", "左右布局", "图文并排",
    ]),
    ("上下图文", [
        "上下图文", "上下分栏", "上下分区", "上图下文",
        "顶部插画底部文字", "顶部插画底部文案", "上方插画下方文字",
    ]),
]
LAYOUT_SUBDIRECTION_NAMES: tuple[str, ...] = tuple(
    name for name, _ in LAYOUT_SUBDIRECTIONS
)


def refine_layout_direction(direction: str | None, corpus: str) -> str | None:
    """把笼统的版式方向「上下左右图文」细分成具体的分栏方向。

    只改这一族方向的**输出名**，不参与打分、不影响其它方向的判定 ——
    这样判定行为与细分前完全一致，只是落盘目录更具体。
    语料里找不到任何分栏证据时原样返回（继续按兜底路径走）。
    """
    if not direction or direction != "上下左右图文":
        return direction
    for name, markers in LAYOUT_SUBDIRECTIONS:
        if any(compact_text(marker) in corpus for marker in markers):
            return name
    return direction


def canonical_direction_name(direction: str) -> str:
    return DIRECTION_ALIASES.get(direction, direction)


def canonical_direction_list(directions: list[str]) -> list[str]:
    output: list[str] = []
    for direction in directions:
        canonical = canonical_direction_name(direction)
        if canonical not in output:
            output.append(canonical)
    return output


GLOBAL_DERIVED_DIRECTION = "秒思自动衍生"
LEGACY_GENERATED_DIRECTION = "妙思自动衍生"
for product, product_directions in list(PRODUCT_DIRECTIONS.items()):
    product_directions = canonical_direction_list(product_directions)
    if GLOBAL_DERIVED_DIRECTION not in product_directions:
        product_directions.append(GLOBAL_DERIVED_DIRECTION)
    if LEGACY_GENERATED_DIRECTION not in product_directions:
        product_directions.append(LEGACY_GENERATED_DIRECTION)
    PRODUCT_DIRECTIONS[product] = product_directions

PRODUCTS = list(PRODUCT_DIRECTIONS)
DIRECTIONS = sorted({direction for values in PRODUCT_DIRECTIONS.values() for direction in values})

# 妙思、有米云输出“业务线/方向”；热云的保险素材共用这套方向规则，
# 非保险素材继续按“产品/方向”，避免改变热云其他业务的既有目录。
EXTERNAL_BUSINESS_PLATFORMS = {"admuse", "appgrowing"}
SHARED_INSURANCE_DIRECTION_PLATFORMS = EXTERNAL_BUSINESS_PLATFORMS | {"reyun"}
SUPPORTED_PLATFORMS = {"admuse", "appgrowing", "internal", "reyun"}
BUSINESS_LINES = ("保险", "小卡", "卡券", "存量", "APP")
PRODUCT_BUSINESS_LINES: dict[str, str] = {
    "百万防癌险": "保险",
    "百万医疗险": "保险",
    "京东保": "保险",
    "联通19元200G": "小卡",
    "QQ阅读": "卡券",
    "中国移动APP-签到有礼+10元话费": "存量",
    "百度": "APP",
    "百度网盘": "APP",
    "百度极速版": "APP",
    "快手": "APP",
    "快手极速版": "APP",
    "淘宝联盟": "APP",
    "喜番免费短剧": "APP",
    "红果短剧": "APP",
    "抖音商城": "APP",
    "小红书": "APP",
    "闲鱼": "APP",
    "今日头条": "APP",
}
BUSINESS_LINE_RULES: dict[str, list[tuple[str, int]]] = {
    "保险": [
        ("保险服务", 28), ("百万医疗险", 22), ("百万防癌险", 22),
        ("医疗险", 15), ("防癌险", 15), ("保险产品", 12), ("投保", 8),
        ("保单", 8), ("承保", 8), ("理赔", 7), ("免赔额", 7),
    ],
    "小卡": [
        ("小卡", 24), ("流量卡", 22), ("手机卡", 20), ("电话卡", 20),
        ("sim卡", 18), ("联通19元200g", 24), ("19元月租", 14),
        ("200g高速流量", 14), ("大流量套餐", 10),
    ],
    "卡券": [
        ("卡券", 24), ("优惠券", 20), ("代金券", 20), ("兑换券", 18),
        ("会员权益", 16), ("权益月月领", 16), ("会员n选1", 14),
        ("月卡", 10), ("周卡", 10),
    ],
    "存量": [
        ("存量", 24), ("老用户", 18), ("用户召回", 18), ("回流", 14),
        ("签到有礼", 18), ("签到领话费", 18), ("任务中心", 12),
        ("连续签到", 12), ("活跃用户", 12),
    ],
    "APP": [
        ("百度app", 18), ("百度网盘", 18), ("百度极速版", 18),
        ("快手app", 18), ("快手极速版", 18), ("淘宝联盟", 18),
        ("喜番免费短剧", 18), ("红果短剧", 18), ("抖音商城", 18), ("小红书", 18),
        ("应用商店", 10), ("app界面", 10),
        # 2026-09-21 补：一批电商/搜索类素材的可见文字里有"快手搜索""快手精选好物"、
        # 画面是快手电商促销版式，但原来只认"快手app"，导致业务线得分全为 0、
        # 整批 233 张被判无法归类。这里补上画面内可直接观察到的快手电商类证据。
        ("快手搜索", 14), ("快手精选好物", 12), ("快手小店", 16),
        # 画面文案"上快手，低价好物带回家"里的显式站名
        ("上快手", 14),
        # 2026-09-22 按策略中心补的两个 APP 产品（只收显式品牌证据，避免蹭到"头条"
        # 这类媒体名：内部系统素材名里的"头条"是投放媒体，不是今日头条这个产品）
        ("闲鱼", 16), ("闲鱼app", 18), ("闲鱼logo", 12),
        ("今日头条", 18), ("今日头条logo", 12), ("头条app", 16),
    ],
}

# 模型只返回证据；以下规则才是产品分类的唯一决策来源。
# 权重越高，证据越具有排他性。负权重用于相似产品边界。
PRODUCT_RULES: dict[str, dict[str, list[tuple[str, int]]]] = {
    "QQ阅读": {
        "positive": [
            ("qq阅读", 14), ("权益月月领", 9), ("会员n选1", 9),
            ("视频网站会员", 7), ("热门会员权益", 7), ("会员权益办理", 7),
        ],
        "negative": [("淘宝", 8), ("快手", 8), ("百度", 8)],
    },
    "百度": {
        "positive": [
            ("百度app", 14), ("百度一下", 12), ("du熊掌logo", 8),
            ("百度", 7), ("蓝色du熊掌", 8),
        ],
        "negative": [
            ("百度极速版", 20), ("极速金", 16), ("红色极速角标", 14),
            ("百度网盘", 24), ("百度网盘logo", 20), ("网盘超级会员", 16),
        ],
    },
    "百度网盘": {
        "positive": [
            ("百度网盘", 22), ("百度网盘logo", 18), ("百度云盘", 18),
            ("蓝红三角网盘logo", 16), ("网盘超级会员", 14), ("网盘会员", 12),
            ("云存储", 8), ("网盘空间", 8), ("文件列表", 6),
            ("上传文件", 6), ("下载文件", 6), ("svip", 8),
        ],
        "negative": [
            ("百度极速版", 22), ("极速金", 16), ("红色极速角标", 14),
            ("百度一下", 12),
        ],
    },
    "百度极速版": {
        "positive": [
            ("百度极速版", 16), ("极速金", 14), ("红色极速角标", 12),
            ("极速版底部角标", 12), ("极速角标", 10), ("看视频赚现金红包", 9),
            ("领现金", 7), ("随时可提现", 7), ("du熊掌logo", 6),
        ],
        "negative": [("百度网盘", 22), ("百度网盘logo", 18), ("网盘超级会员", 14)],
    },
    "百万防癌险": {
        "positive": [
            ("百万防癌险", 18), ("防癌险", 14), ("恶性肿瘤", 10),
            ("癌症保障", 9), ("癌症", 7), ("肿瘤", 7),
        ],
        "negative": [("百万医疗险", 20), ("大病小病均可保障", 12)],
    },
    "百万医疗险": {
        "positive": [
            ("百万医疗险", 18), ("医疗险", 12), ("大病小病均可保障", 12),
            ("免赔额", 10), ("住院医疗", 8), ("医疗保障", 7), ("住院", 4),
            ("保险规划", 5), ("守护健康", 5), ("保障为你护航", 4),
        ],
        # 两者证据高度重叠；出现该产品专属证据时让位，否则会被误并进宽泛产品。
        "negative": [
            ("防癌险", 14), ("恶性肿瘤", 12),
            ("京东保", 20), ("jda", 16), ("京东集团旗下保险经纪平台", 18),
        ],
    },
    # 词表取自真实证据统计：按 brand_keywords 聚合计数，挑高频且互斥的词进表。
    "京东保": {
        "positive": [
            ("京东保", 20),                  # 同时覆盖 JDA京东保 / 京东保Logo / 京东保文字
            ("京东集团旗下保险经纪平台", 18),   # 京东保专属 slogan
            ("jda", 16),                     # Logo 字母，覆盖 JDA / JDA京东保Logo
            ("泰康在线和京东保联合推出", 14),
            # 单出现「京东」不给分够用（低于阈值 9），只是兜底弱证据
            ("京东", 6),
        ],
        "negative": [
            ("百万防癌险", 16), ("防癌险", 12), ("恶性肿瘤", 10),
        ],
    },
    "快手": {
        "positive": [
            ("快手app", 14), ("快手logo", 11), ("橙色相机logo", 10),
            ("橙红色相机logo", 9), ("橙色双环logo", 11),
            ("橙色圆角方形双环白纹logo", 11), ("橙色方形白色摄像机图案logo", 11),
            ("橙色方形白色摄像机logo", 11), ("红色圆角方形白摄像机logo", 12),
            ("纯橙色", 10), ("纯色相机logo", 10),
            # 画面内可见的快手电商/搜索类证据（比裸"快手"更强：说明是快手站内引流）
            ("快手搜索", 12), ("快手精选好物", 10), ("快手小店", 14),
            ("快手", 7),
        ],
        "negative": [
            ("快手极速版", 22), ("闪电角标", 14), ("极速版角标", 16),
            # 渐变是极速版的形状特征；"粉色渐变播放图标"这类也应压低通用快手得分，
            # 避免短剧类素材的播放按钮被读成快手相机 Logo。
            ("渐变", 12),
        ],
    },
    "快手极速版": {
        "positive": [
            ("快手极速版", 18), ("极速版角标", 14), ("闪电角标", 12),
            ("红橙渐变相机logo", 10), ("橙色渐变", 12), ("渐变相机logo", 12),
            ("快手logo", 4),
        ],
        "negative": [("纯橙色无渐变", 12), ("纯色相机logo", 10)],
    },
    "淘宝联盟": {
        "positive": [
            ("淘宝联盟", 18), ("淘宝logo", 12), ("淘客", 10),
            ("淘宝", 7), ("商品价格", 4), ("购买按钮", 4), ("推广界面", 5),
        ],
        "negative": [],
    },
    "喜番免费短剧": {
        "positive": [
            ("喜番免费短剧", 18), ("喜番", 15), ("免费短剧", 9),
            ("短剧logo", 9), ("短剧", 5), ("看短剧领金币", 8),
            ("新用户满1元提现", 8), ("赚外快", 7),
        ],
        "negative": [],
    },
    "红果短剧": {
        # 红果短剧的图标是"橙色圆角方块+白色播放三角"，与快手"相机"形Logo极易混淆，
        # 视觉模型常读成"橙色相机Logo"，因此不能依赖 Logo 形状，改以文案与文件名为准。
        "positive": [
            ("红果短剧", 18), ("红果", 15), ("该故事纯属虚构", 8),
        ],
        "negative": [],
    },
    # 2026-09-22 按策略中心补的两个 APP 产品。
    # 只收显式品牌证据：内部系统素材名里的"头条"是投放媒体（今日头条这个媒体的广告位），
    # 不是"今日头条"这个拉新产品，所以不把裸"头条"当证据，免得把保险素材误判成 APP 素材。
    "闲鱼": {
        "positive": [
            ("闲鱼", 18), ("闲鱼app", 18), ("闲鱼logo", 12), ("闲鱼小法庭", 10),
        ],
        "negative": [],
    },
    "今日头条": {
        "positive": [
            ("今日头条", 18), ("今日头条logo", 12), ("头条app", 16),
        ],
        "negative": [],
    },
    "中国移动APP-签到有礼+10元话费": {
        "positive": [
            ("中国移动logo", 16), ("中国移动", 12), ("签到有礼", 10),
            ("签到领话费", 10), ("10元话费", 9), ("至高10元话费", 10),
        ],
        "negative": [],
    },
    "抖音商城": {
        "positive": [
            ("抖音商城", 18), ("抖音logo", 16), ("抖音", 9),
            ("购物袋图标", 7),
        ],
        "negative": [],
    },
    "小红书": {
        "positive": [
            ("小红书", 18), ("小红书logo", 16), ("生活兴趣社区", 8),
            ("美妆博主", 8), ("美妆教程", 8),
        ],
        "negative": [],
    },
    "联通19元200G": {
        "positive": [
            ("联通19元200g", 20), ("中国联通", 18), ("chinaunicom", 14),
            ("19元", 8), ("200g", 8), ("高速流量", 8),
        ],
        "negative": [],
    },
}

# 方向规则会在系统已经确定产品后，仅在该产品允许的方向中比较。
DIRECTION_RULES: dict[str, list[tuple[str, int]]] = {
    "插画": [
        ("卡通插画", 14), ("家庭插画", 14), ("人物插画", 13),
        ("插画主导", 13), ("插画场景", 10), ("绘制人物", 8), ("卡通人物", 8),
        ("卡通儿童插画", 14), ("卡通家庭插画", 14), ("卡通男孩", 10),
        ("卡通女孩", 10), ("卡通形象", 9), ("卡通小马", 9), ("卡通", 7),
    ],
    "盾牌": [
        ("大型盾牌", 16), ("盾牌主导", 16), ("盾牌图标", 16),
        ("锁形盾牌", 12), ("盾牌", 11),
    ],
    "上下左右图文": [
        ("多区块图文", 14), ("上下左右分区", 14), ("左右分栏", 14),
        ("左文右图", 14), ("左图右文", 14), ("右文左图", 14),
        ("四区块", 10), ("组合信息区块", 9), ("左右布局", 9),
        # 2026-09-22：原来靠 DIRECTION_ALIASES 把「左右图文」并进来，现在那个别名去掉了
        # （它成了细分后的正式方向名），这里显式保留同权重，判定行为不变。
        ("左右图文", 14),
    ],
    "图标": [
        ("权益图标", 13), ("功能图标", 12), ("图标主导", 14),
        ("中心图标主导", 16), ("环绕式图标布局", 15), ("图标环绕布局", 15),
        ("多个图标", 9), ("图标阵列", 11), ("图标区", 9),
    ],
    "小程序": [
        ("小程序卡片界面", 16), ("小程序界面", 15), ("小程序", 14),
        ("头像卡片", 10), ("卡片界面", 8), ("登录界面", 5),
    ],
    "诱导类": [
        ("登录按钮主导", 16), ("一键登录", 14), ("立即登录", 12),
        ("领取按钮主导", 12), ("极简按钮界面", 10),
    ],
    "月亮": [
        ("发光月亮", 16), ("圆月", 14), ("月亮主导", 15), ("月牙", 13),
        ("月相", 12), ("多个月牙", 14), ("月亮", 10), ("夜景", 4),
        ("宇宙星球场景", 10), ("星空宇宙", 10), ("太空场景", 9),
        ("发光水晶球", 10), ("透明球体", 9), ("水面波纹", 8),
        ("抽象液体场景", 8), ("光效场景", 7), ("星空背景", 7),
        ("宇宙星空", 10), ("太空星云", 10), ("星空漩涡", 10),
        ("发光行星", 10), ("星球", 8),
        ("九宫格", 18), ("九宫格排列", 20), ("九宫格环绕", 20),
        ("3x3排列", 20), ("八个圆点按钮", 18), ("八个圆点", 17),
        ("气泡状圆形图标", 17), ("小型透明圆点", 16), ("圆点按钮", 15),
        ("同心圆盘", 14),
    ],
    "自动衍生": [
        ("qq阅读二次组合", 18), ("嵌套广告截图", 12), ("原素材面板", 12),
        ("手机号确认", 10), ("惊喜放送", 9), ("二次组合模板", 8),
    ],
    "大字报": [
        ("大字报", 15), ("大号文字主导", 13), ("文字占比极高", 10),
    ],
    # 2026-09-22 细分「上下左右图文」：这一族原来是三种版式混在一个方向里，
    # 落盘只能进一个笼统目录（且分类表里没有它，191 条被门控成「未知」）。
    # 拆成 多区块/左右/上下 三种；证据词只用画面里能直接观察到的版式描述。
    "上下图文": [
        ("上下图文", 15), ("上下分栏", 14), ("上下分区", 12), ("上图下文", 14),
        ("顶部插画底部文字", 12), ("顶部插画底部文案", 12), ("上方插画下方文字", 12),
    ],
    "多区块图文": [
        ("多区块图文", 15), ("上下左右分区", 12), ("四区块", 12),
        ("组合信息区块", 11), ("权益卡片列表", 10), ("信息卡片", 10), ("多行权益", 10),
    ],
    "家庭亲子插画": [
        ("家庭亲子插画", 18), ("亲子插画", 15), ("父母和儿童", 12),
        ("家庭场景", 8), ("亲子", 8), ("儿童", 4),
    ],
    "左右图文": [
        ("左右分栏", 15), ("左文右图", 14), ("左图右文", 14), ("纵向两栏", 10),
    ],
    "老歌": [
        ("经典老歌", 18), ("老歌", 16), ("90年代歌曲", 14),
        ("免费听歌", 10), ("歌曲搜索", 8),
    ],
    "旅游": [
        ("旅游", 12), ("旅游景点", 14), ("旅行攻略", 14), ("出游季", 13),
        ("出游攻略", 16), ("游乐园场景", 14), ("万圣节主题乐园", 14),
        ("自然风光", 11), ("自然风景", 11), ("户外自然风景", 12),
        ("户外旅游场景", 14), ("湖泊风景", 11), ("旅游目的地", 12), ("风景背景", 8),
        ("地标风景", 13), ("山水风景", 10), ("旅行", 8),
    ],
    "旅游攻略": [
        ("旅游攻略", 20), ("旅行攻略", 16), ("旅游景点", 12),
        ("地标风景", 10), ("景点", 7), ("旅行", 6),
    ],
    "美女": [
        ("女性人物", 14), ("美女", 14), ("女性人物和红包", 15),
        ("女性人物和现金收益", 15), ("女性人物自拍", 13), ("美女穿搭", 15),
    ],
    "美女网赚": [
        ("美女网赚", 18), ("女性人物和红包", 15), ("女性人物和现金收益", 15),
        ("美女和金币", 13), ("美女穿搭", 12),
    ],
    "萌宠": [
        ("萌宠", 16), ("宠物主导", 14), ("猫咪", 11), ("狗狗", 11),
        ("白狮幼崽", 13), ("猫", 7), ("狗", 7), ("宠物特写", 10),
    ],
    "网赚": [
        ("现金收益", 13), ("提现", 12), ("赚钱", 10), ("红包", 7),
        ("金币", 6), ("现金红包", 9), ("收益数字", 7), ("领现金", 8),
    ],
    "文字大字报": [
        ("文字大字报", 17), ("大号文字主导", 13), ("文字占比极高", 11),
    ],
    "幸福生活": [
        ("幸福生活", 16), ("生活方式场景", 12), ("休闲生活", 10),
        ("节庆活动", 9), ("日常活动", 8),
    ],
    "一分购": [
        ("一分钱购", 18), ("1分钱购", 18), ("1分购", 17),
        ("一分购", 17), ("0.01元", 15), ("超低价购买", 8),
    ],
    "妙思自动衍生": [
        ("妙思自动衍生", 18), ("房间装饰背景", 12), ("嵌套广告截图", 11),
        ("原广告截图", 11), ("二次组合模板", 9),
    ],
    "秒思自动衍生": [
        ("完整原素材嵌入", 24), ("内嵌完整广告图", 24),
        ("画中画广告", 22), ("画中画原素材", 22),
        ("嵌套广告截图", 18), ("原广告截图", 18),
        ("原素材面板", 16), ("二次组合模板", 14),
    ],
    "短剧": [
        ("短剧", 15), ("影视剧照", 12), ("剧情画面", 11), ("影视人物", 8),
    ],
    "签到有礼": [
        ("签到有礼", 17), ("签到界面", 14), ("签到奖励", 13),
        ("话费奖励", 11), ("签到", 7),
    ],
    # 2026-09-22 按策略中心补的方向。策略中心的方向名比本地点更具体：不是笼统的
    # 「一分购／签到有礼」，而是「0.01元／0.1元」和「签到有礼-100元话费+2G流量」。
    # 只收字面证据，不猜语义 —— 宁可判不出，也不误伤。
    "0.01元": [
        ("0.01元", 18), ("0.01元起", 18), ("0.01元购买", 20),
    ],
    "0.1元": [
        ("0.1元", 18), ("0.1元购买", 20),
    ],
    "签到有礼-100元话费+2G流量": [
        ("100元话费+2g流量", 24), ("100元话费+2G流量", 24), ("100元话费", 18),
    ],
    "签到有礼-10元话费+2G流量": [
        ("10元话费+2g流量", 22), ("10元话费+2G流量", 22), ("至高10元话费", 20),
    ],
    "综合卖点": [
        ("综合卖点", 20), ("卖点罗列", 14), ("卖点列表", 14),
    ],
    "美妆": [
        ("小红书美妆学院", 18), ("美妆教程", 16), ("美妆博主", 14),
        ("化妆技巧", 13), ("约会妆容", 13), ("女性护肤", 11),
        ("化妆品", 9), ("眼影", 8), ("口红", 8),
    ],
    "美食": [
        ("美食", 16), ("食谱", 14), ("烹饪", 13), ("菜谱", 13),
        ("餐厅美食", 12), ("食物摄影", 11), ("甜品", 9), ("小吃", 8),
    ],
    "低价好物": [
        ("低价好物", 20), ("好物低价", 18), ("超值好物", 16),
        ("全网低价", 15), ("9.9包邮", 14), ("限时低价", 12),
    ],
    "快手电商": [
        ("快手商城", 20), ("快手小店", 20), ("小店后台", 16),
        ("商家经营", 15), ("流量扶持", 14), ("开店", 12), ("入驻", 12),
    ],
    "电商": [
        ("电商平台", 18), ("电商服务", 16), ("商品详情", 13),
        ("商品橱窗", 12), ("购物平台", 12), ("商家经营", 11),
    ],
    "内容创意": [
        ("内容创意", 18), ("创意玩法", 14), ("趣味互动", 12),
        ("生活灵感", 11), ("内容推荐", 10),
    ],
    "官方样式": [
        ("刷快手", 18), ("快手官方", 16), ("快手搜索", 10),
        ("沉浸式", 9), ("奔赴", 9),
    ],
    "猎奇": [
        ("猎奇", 18), ("奇闻", 15), ("不可思议", 13),
        ("罕见", 11), ("冷知识", 10),
    ],
    "朋友圈": [
        ("朋友圈", 18), ("中国联通", 10), ("19元月租", 14), ("200g高速流量", 14),
        ("官方套餐", 11), ("不限速", 8), ("随停随换", 8),
    ],
    "未细分": [],
}


def merge_direction_alias_rules(rules: dict[str, list[tuple[str, int]]]) -> None:
    """把细分类识别词合并进最终统一方向，并移除旧候选名。"""
    for alias, canonical in DIRECTION_ALIASES.items():
        alias_rules = rules.pop(alias, [])
        canonical_rules = rules.setdefault(canonical, [])
        for rule in alias_rules:
            if rule not in canonical_rules:
                canonical_rules.append(rule)


merge_direction_alias_rules(DIRECTION_RULES)

# 外部素材按业务线归档时使用较粗的方向名，但底层仍复用同一套方向规则。
# 同一个内部方向可以由不同业务线用**不同的外部名**聚合，靠 BUSINESS_LINE_DIRECTIONS
# 限定各自能用哪些名字（例如保险沿用粗粒度的「宠物/上下左右图文」，
# APP 按 2026-09-28 业务侧清单用「萌宠」并把版式细分成三种）。
EXTERNAL_DIRECTION_SOURCES: dict[str, tuple[str, ...]] = {
    direction: (direction,)
    for direction in DIRECTION_RULES
    if direction != "未细分"
}
for external_name, source_names in {
    # —— 保险沿用（2026-09-22 之前的粗粒度聚合，不要动）——
    "上下左右图文": ("上下左右图文", "左右图文"),
    "宠物": ("萌宠",),
    # —— 2026-09-28 业务侧确认的 APP 方向清单 ——
    # 业务侧叫「萌宠」，不再叫「宠物」
    "萌宠": ("萌宠",),
    # 业务侧叫「购物」；本地点原来叫「低价好物」，同一件事。
    # 0.1元 属低价促销，一并归购物（0.01元 才是「一分购」）。
    "购物": ("低价好物", "0.1元"),
    # 0.01元 与「一分购」是同一件事
    "一分购": ("一分购", "0.01元"),
    # 「旅游攻略」并回「旅游」
    "旅游": ("旅游", "旅游攻略"),
    # 「电商」并入「快手电商」：业务侧 2026-09-28 定，招商/电商经营类只留这一个方向
    "快手电商": ("快手电商", "电商"),
    # —— 通用 ——
    "大字报": ("大字报", "文字大字报"),
}.items():
    for source_name in source_names:
        EXTERNAL_DIRECTION_SOURCES.pop(source_name, None)
    EXTERNAL_DIRECTION_SOURCES[external_name] = source_names

# 上面把「左右图文」摘给了保险的「上下左右图文」，但 APP 要它自己独立成方向，
# 这里补回来（每个业务线只会给自己的白名单里的名字打分，两者不会互相干扰）。
for layout_direction in ("多区块图文", "左右图文", "上下图文"):
    EXTERNAL_DIRECTION_SOURCES[layout_direction] = (layout_direction,)

# 违规拦截的默认落点；规则文件里可以改成别的方向。
# 定义在这里是因为 BUSINESS_LINE_DIRECTIONS 要引用它。
VIOLATION_DIRECTION = "违规素材"

# ---------------------------------------------------------- 界面伪装元素（2026-10-10）
# 业务侧：违规素材要覆盖「播放按钮、关闭按钮等模仿系统界面交互的图」。
#
# 为什么不能直接加关键词到全语料匹配：这些词在**正常素材**里也会出现。
# 实测 1114 条内部素材里就有「绿色弧形进度条」（装饰弧线）、
# 商品文案「静音滑轮」、「播放按钮图标」（红果短剧/腾讯动漫的品牌 Logo）。
# 扫全语料必然误伤。
#
# 因此分两步走：
#   1) 提示一个新字段 ui_mimicry_elements，让模型**明确判定**哪些是伪装控件，
#      并在提示词里写死排除项（品牌 Logo 内部的图形、装饰性圆点、卖点文案、广告自身按钮）。
#   2) 违规规则用 match_fields 只在该字段里匹配，不看其余语料 —— 零误伤。
UI_MIMICRY_FIELD = "ui_mimicry_elements"
UI_MIMICRY_VALUES: tuple[str, ...] = (
    "播放按钮",
    "关闭按钮",
    "全屏按钮",
    "静音按钮",
    "音量条",
    "进度条",
    "返回键",
    "系统弹窗",
    "假搜索框",
    "假输入框",
    "系统状态栏",
    "系统导航栏",
)
# 只有 match_fields 声明为 [UI_MIMICRY_FIELD] 的规则才用得到它；
# 旧素材（缓存里没这个字段）解析出空列表，规则自然不命中，行为与改动前一致。

# ---------------------------------------------------------- 违禁元素（2026-10-10）
# 业务侧：国旗、国徽等涉政元素，以及涉黄内容，也算违规素材；**插画类尤其要注意**。
#
# 与界面伪装同理，不能只加关键词：这些词在正常语境里也会出现
# （业务侧本来就有「美女类」素材标签、文案里也可能出现「国」字），
# 所以同样走「专门字段 + match_fields 限定」。
#
# ⚠️ 与 UI_MIMICRY 不同的风险：模型对涉政涉黄内容有**回避倾向**，
# 可能干脆不描述、或用极模糊的说法，导致规则空转。因此提示词里必须：
#   1) 说明这是广告发布前的合规审查场景（如实记录事实，供人工复核）
#   2) 明确要求客观中立、不带评价与延伸
#   3) 点名插画/卡通/Q版风格**同样要判断**（业务侧特别强调这点）
# 这一步的效果只能靠小样本实跑验证，见 tests/test_violation_rules.py 的说明。
PROHIBITED_FIELD = "prohibited_elements"
PROHIBITED_VALUES: tuple[str, ...] = (
    # 涉政：国家象征与国家机关符号（《广告法》第九条禁止商业广告使用）
    "国旗",
    "国徽",
    "党徽",
    "国家领导人形象",
    "人民币图案",
    "军旗军徽",
    "国家机关标识",
    # 涉黄：低俗内容
    "色情裸露",
    "性暗示",
    "低俗挑逗",
)

# 所有「按字段限定匹配」的违规证据字段。新增这类字段时**必须登记在这里**，
# 否则 violation_field_corpora() 不会为它生成语料，规则会静默失效。
VIOLATION_FIELD_NAMES: tuple[str, ...] = (UI_MIMICRY_FIELD, PROHIBITED_FIELD)

# 2026-09-28 业务侧确认的 APP 方向清单：
#   内容方向 + 3 个版式方向（09-22 从「上下左右图文」细分出来的三种）
#   + 妙思自动衍生素材的专用标记（引擎机制，不是业务方向）。
# 「快手电商」是业务侧后续补的：开店/商家入驻/流量扶持这类**招商广告**要单独成目录，
# 不能混进「内容创意」；原来的「电商」并入它，不再单独存在。
APP_DIRECTIONS: tuple[str, ...] = (
    "网赚",
    "萌宠",
    "老歌",
    "旅游",
    "美食",
    "短剧",
    "一分购",
    "官方样式",
    "内容创意",
    "购物",
    "快手电商",
    "多区块图文",
    "左右图文",
    "上下图文",
)
# APP 下**不保留「未知」**：方向判不出来、或落在清单之外，一律归到这里。
APP_FALLBACK_DIRECTION = "内容创意"

# 妙思自动衍生素材的专用标记。两条路径叫法不同：
#   business_line 模式 → 秒思自动衍生 / 妙思自动衍生
#   collection_taxonomy 模式 → 分类表的 derived_direction（妙思衍生）
# 这些是引擎机制、不是业务方向，都要保留，不能被收敛成「内容创意」。
APP_DERIVED_DIRECTIONS: tuple[str, ...] = (
    GLOBAL_DERIVED_DIRECTION,
    LEGACY_GENERATED_DIRECTION,
    "妙思衍生",
    "自动衍生",
)

BUSINESS_LINE_DIRECTIONS: dict[str, set[str]] = {
    "保险": {
        "插画", "图标", "上下左右图文", "宠物", "大字报", "月亮", "朋友圈",
        # 违规拦截的目标方向（规则见 userdata/violation_rules.json）。
        # 它没有打分规则、不参与常规判定，只由 match_violation_rule 写入；
        # 登记在这里是为了让人一眼看到保险确实有这个方向。
        VIOLATION_DIRECTION,
        GLOBAL_DERIVED_DIRECTION,
    },
    "APP": set(APP_DIRECTIONS) | set(APP_DERIVED_DIRECTIONS),
}


def apply_app_direction_allowlist(direction: str, business_line: str) -> str:
    """APP 业务线只保留业务侧确认的方向清单，其余一律归「内容创意」。

    2026-09-28 业务侧定调：APP 方向 = 10 个内容方向 + 3 个版式方向 + 衍生标记，
    **不再保留「未知」** —— 方向判不出来、或落在清单之外（含门控后的「未知」），
    全部落 APP_FALLBACK_DIRECTION。

    两条产出路径都要过这一关：外部平台走 business_line 模式，
    内部系统走 collection_taxonomy 模式，两边都必须收敛到同一份清单。
    """
    if business_line != "APP":
        return direction
    if direction in BUSINESS_LINE_DIRECTIONS["APP"]:
        return direction
    return APP_FALLBACK_DIRECTION


# ============================ 违规素材拦截 ============================
#
# 业务侧 2026-09-29：保险的「图标」素材里，凡是画面出现箭头的，都不该按图标归档，
# 要单独落到「违规素材」方向。
#
# 规则**不写死在代码里**：放在 userdata/violation_rules.json，业务侧可以自己改
# （关键词、业务线、落到哪个方向都能调），改完即时生效，不用改代码也不用重启。
# 文件不存在会自动按默认规则生成一份。

VIOLATION_RULES_ENV = "MATERIAL_DOWNLOADER_VIOLATION_RULES"
DEFAULT_VIOLATION_RULES = {
    "version": 3,
    "note": (
        "违规素材拦截规则。命中即优先归到指定方向，不再参与常规方向判定。"
        "keywords 按子串匹配 AI 提取到的关键词；business_lines 为空表示所有业务线都拦；"
        "include_uncertain=true 时连 AI 标注「不确定」的内容也参与匹配（默认 false，避免误伤）。"
        "match_fields 指定只在哪几个证据字段里匹配（为空则扫全语料）——"
        "像「进度条」「播放按钮」这类词在正常素材里也会出现（装饰弧线、品牌 Logo），"
        "所以这类规则必须限定字段。"
    ),
    "rules": [
        {
            "id": "insurance-arrow",
            "name": "保险-箭头素材",
            "enabled": True,
            "business_lines": ["保险"],
            "direction": VIOLATION_DIRECTION,
            "keywords": ["箭头"],
            "reason": "画面出现箭头，属违规素材",
            "include_uncertain": False,
        },
        {
            # 业务侧 2026-10-10：模仿系统界面交互的图（播放按钮、关闭按钮等）算违规素材。
            "id": "ui-mimicry",
            "name": "界面伪装素材",
            "enabled": True,
            # 空 = 所有业务线都拦（保险 / APP / 卡券…）
            "business_lines": [],
            "direction": VIOLATION_DIRECTION,
            "keywords": list(UI_MIMICRY_VALUES),
            # 关键：只在模型明确判定为「伪装控件」的字段里匹配，不看其余语料。
            # 该字段由 UI_MIMICRY_POLICY_ADDENDUM 约束，模型只在真的看到伪装控件时才填。
            "match_fields": [UI_MIMICRY_FIELD],
            "reason": "画面模仿系统/App 界面控件，属违规素材",
            "include_uncertain": False,
        },
        {
            # 业务侧 2026-10-10：国旗、国徽等涉政国家象征，以及涉黄低俗内容，
            # 也算违规素材；**插画类尤其要注意**（该要求写在
            # PROHIBITED_POLICY_ADDENDUM 里：卡通/Q版风格同样要判断）。
            "id": "prohibited-content",
            "name": "违禁元素素材",
            "enabled": True,
            "business_lines": [],
            "direction": VIOLATION_DIRECTION,
            "keywords": list(PROHIBITED_VALUES),
            "match_fields": [PROHIBITED_FIELD],
            "reason": "画面含涉政国家象征或涉黄低俗内容，属违规素材",
            "include_uncertain": False,
        },
    ],
}


def _default_violation_rules_file() -> Path:
    """<项目根>/userdata/violation_rules.json（可用环境变量覆盖）。"""
    override = os.environ.get(VIOLATION_RULES_ENV, "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "userdata" / "violation_rules.json"


_VIOLATION_RULES_CACHE: dict[str, Any] | None = None


def normalize_violation_rules(value: Any) -> dict[str, Any]:
    """把外部配置归一化，坏规则直接丢掉而不是让整批分类崩掉。"""
    if not isinstance(value, dict):
        return {"version": 1, "note": DEFAULT_VIOLATION_RULES["note"], "rules": []}
    rules: list[dict[str, Any]] = []
    for raw in value.get("rules") or []:
        if not isinstance(raw, dict):
            continue
        direction = str(raw.get("direction") or "").strip()
        keywords = [
            str(item).strip()
            for item in (raw.get("keywords") or [])
            if str(item).strip()
        ]
        if not direction or not keywords:
            # 没有方向或没有关键词的规则没法用，跳过
            continue
        business_lines = [
            str(item).strip()
            for item in (raw.get("business_lines") or [])
            if str(item).strip()
        ]
        # 指定只在哪些证据字段里匹配；为空表示扫全语料（老规则的行为）。
        match_fields = [
            str(item).strip()
            for item in (raw.get("match_fields") or [])
            if str(item).strip()
        ]
        rules.append({
            "id": str(raw.get("id") or f"rule-{len(rules) + 1}").strip(),
            "name": str(raw.get("name") or direction).strip(),
            "enabled": raw.get("enabled", True) is not False,
            "business_lines": business_lines,
            "direction": direction,
            "keywords": keywords,
            "match_fields": match_fields,
            "reason": str(raw.get("reason") or "").strip(),
            # 默认只匹配「已确认」的证据；打开后才把 uncertain 里的内容也算进来。
            # 实测有素材的箭头只出现在 uncertain（AI 自己都写了「含义不明确」），
            # 默认不拦是为了避免误伤，需要时把这个开关打开。
            "include_uncertain": raw.get("include_uncertain") is True,
        })
    return {
        "version": int(value.get("version") or 1),
        "note": str(value.get("note") or DEFAULT_VIOLATION_RULES["note"]),
        "rules": rules,
    }


def load_violation_rules(*, reload: bool = False) -> dict[str, Any]:
    """读取违规拦截规则；文件不存在时按默认规则生成一份再读。"""
    global _VIOLATION_RULES_CACHE
    if _VIOLATION_RULES_CACHE is not None and not reload:
        return _VIOLATION_RULES_CACHE

    path = _default_violation_rules_file()
    if not path.is_file():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(DEFAULT_VIOLATION_RULES, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            # 写不了（只读目录等）也不影响运行，直接用默认值
            _VIOLATION_RULES_CACHE = normalize_violation_rules(DEFAULT_VIOLATION_RULES)
            return _VIOLATION_RULES_CACHE

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = DEFAULT_VIOLATION_RULES
    _VIOLATION_RULES_CACHE = normalize_violation_rules(raw)
    return _VIOLATION_RULES_CACHE


def violation_field_corpora(evidence: dict[str, Any]) -> dict[str, str]:
    """按字段拆分的语料，供规则用 match_fields 精确定位。

    这些字段的词在正常素材的其它字段里也会出现，扫全语料会误伤：
    - ui_mimicry_elements：「进度条」「播放按钮」也是装饰弧线、品牌 Logo、商品卖点文案；
    - prohibited_elements：「国旗」「国」等词也可能是品牌标识、正常文字（国漫）。

    新增这类字段时，记得登记进 VIOLATION_FIELD_NAMES —— 否则这里不会为它生成语料，
    match_fields 规则会静默失效（check_rules 的 C7 能查出来）。
    """
    corpora: dict[str, str] = {}
    for field in VIOLATION_FIELD_NAMES:
        values = evidence.get(field)
        if not isinstance(values, (list, tuple)):
            continue
        joined = compact_text("|".join(str(item) for item in values))
        if joined:
            corpora[field] = joined
    return corpora


def match_violation_rule(
    business_line: str,
    corpus: str,
    uncertain_corpus: str = "",
    field_corpora: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    """命中则返回 (目标方向, 原因)。多条命中按配置顺序取第一条。

    corpus 是**已确认**证据的语料（不含 uncertain）；只有规则显式打开
    include_uncertain 时，才把 uncertain 的内容也纳入匹配。

    rule.match_fields 非空时，只在声明的字段里匹配 —— 字段为空则该规则不适用
    （**不会退化成扫全语料**，否则限定字段就失去意义）。

    ⚠️ 所有调用点都必须传 field_corpora，否则 match_fields 规则永远拿不到语料、
    整条规则静默失效。目前有两条产出路径都要传：
    `classify_keyword_evidence`（外部平台 business_line）与
    `apply_collection_taxonomy`（内部系统 collection_taxonomy）。
    """
    rules = load_violation_rules().get("rules") or []
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        lines = rule.get("business_lines") or []
        if lines and business_line not in lines:
            continue
        match_fields = rule.get("match_fields") or []
        if match_fields:
            haystack = compact_text("|".join(
                (field_corpora or {}).get(field, "") for field in match_fields
            ))
            if not haystack:
                # 这些字段都是空的 → 这条规则本次不适用
                continue
        else:
            haystack = corpus
            if rule.get("include_uncertain") and uncertain_corpus:
                haystack = f"{haystack}|{uncertain_corpus}"
        for keyword in rule.get("keywords") or []:
            marker = compact_text(keyword)
            if marker and marker in haystack:
                reason = rule.get("reason") or rule.get("name") or "命中违规拦截规则"
                return str(rule["direction"]), f"{rule['name']}：命中「{keyword}」"
    return None


def uncertain_text_corpus(evidence: dict[str, Any]) -> str:
    """uncertain 字段的语料（默认不参与匹配，见 match_violation_rule）。"""
    values = evidence.get("uncertain")
    if not isinstance(values, (list, tuple)):
        return ""
    return compact_text("|".join(str(item) for item in values))


def apply_violation_rules(result: dict[str, Any], business_line: str) -> dict[str, Any] | None:
    """违规拦截是**最后一关**：命中就改写方向，优先级高于常规判定。

    两条产出路径（business_line / collection_taxonomy）都要过这一关，
    否则同一个素材会因为走的平台不同而落到不同方向。
    """
    keywords = result.get("keywords")
    if not isinstance(keywords, dict):
        return None
    corpus = keyword_corpus(keywords)
    field_corpora = violation_field_corpora(keywords)
    if not corpus and not field_corpora:
        return None
    matched = match_violation_rule(
        business_line, corpus, field_corpora=field_corpora
    )
    if not matched:
        return None
    direction, reason = matched
    return {"direction": direction, "reason": reason}

# 第一阶段继续使用 2026-07-24 审计表生成时的稳定规则；只有它拒绝时，
# 才进入上面的校准补充规则。这样人工校准能救回 REVIEW，同时不会重写
# 已经通过旧规则自动归档的历史样本。
LEGACY_PRODUCT_DIRECTIONS: dict[str, list[str]] = {
    "QQ阅读": ["插画", "盾牌", "上下左右图文", "图标", "小程序", "诱导类", "月亮", "自动衍生"],
    "百度": ["未细分"],
    "百度极速版": ["网赚"],
    "百万防癌险": ["图标"],
    "百万医疗险": ["插画", "大字报", "家庭亲子插画", "图标", "月亮", "左右图文"],
    "京东保": ["插画", "大字报", "家庭亲子插画", "图标", "盾牌", "月亮", "左右图文", "多区块图文", "上下图文", "内容创意", "综合卖点", "海报"],
    "快手": ["老歌", "旅游攻略", "美女网赚", "萌宠", "网赚", "文字大字报", "幸福生活", "一分购"],
    "快手极速版": ["妙思自动衍生", "网赚", "文字大字报", "一分购"],
    "淘宝联盟": ["未细分"],
    "喜番免费短剧": ["短剧"],
    "红果短剧": ["短剧"],
    "闲鱼": ["导买"],
    "今日头条": ["网赚", "资讯", "追剧"],
    "中国移动APP-签到有礼+10元话费": ["签到有礼"],
}
for product, product_directions in list(LEGACY_PRODUCT_DIRECTIONS.items()):
    product_directions = canonical_direction_list(product_directions)
    if GLOBAL_DERIVED_DIRECTION not in product_directions:
        product_directions.append(GLOBAL_DERIVED_DIRECTION)
    if LEGACY_GENERATED_DIRECTION not in product_directions:
        product_directions.append(LEGACY_GENERATED_DIRECTION)
    LEGACY_PRODUCT_DIRECTIONS[product] = product_directions

LEGACY_PRODUCT_RULES: dict[str, dict[str, list[tuple[str, int]]]] = {
    "QQ阅读": {
        "positive": [
            ("qq阅读", 14), ("权益月月领", 9), ("会员n选1", 9),
            ("视频网站会员", 7), ("热门会员权益", 7), ("会员权益办理", 7),
        ],
        "negative": [("淘宝", 8), ("快手", 8), ("百度", 8)],
    },
    "百度": {
        "positive": [
            ("百度app", 14), ("百度一下", 12), ("du熊掌logo", 8),
            ("百度", 7), ("蓝色du熊掌", 8),
        ],
        "negative": [
            ("百度极速版", 20), ("极速金", 16), ("红色极速角标", 14),
            ("百度网盘", 24), ("百度网盘logo", 20), ("网盘超级会员", 16),
        ],
    },
    "百度极速版": {
        "positive": [
            ("百度极速版", 16), ("极速金", 14), ("红色极速角标", 12),
            ("极速版底部角标", 12), ("看视频赚现金红包", 9), ("du熊掌logo", 4),
        ],
        "negative": [("百度网盘", 22), ("百度网盘logo", 18), ("网盘超级会员", 14)],
    },
    "百万防癌险": {
        "positive": [
            ("百万防癌险", 18), ("防癌险", 14), ("恶性肿瘤", 10),
            ("癌症保障", 9), ("癌症", 7), ("肿瘤", 7),
        ],
        "negative": [("百万医疗险", 20), ("大病小病均可保障", 12)],
    },
    "百万医疗险": {
        "positive": [
            ("百万医疗险", 18), ("医疗险", 12), ("大病小病均可保障", 12),
            ("免赔额", 8), ("住院医疗", 8), ("医疗保障", 7), ("住院", 4),
        ],
        # 两者证据高度重叠，这里必须让位，否则第一阶段就被宽泛产品抢走。
        "negative": [
            ("防癌险", 20), ("恶性肿瘤", 12),
            ("京东保", 20), ("jda", 16), ("京东集团旗下保险经纪平台", 18),
        ],
    },
    # 2026-10-15 业务侧要求单独成产品。第一阶段也要有，否则会被百万医疗险抢走。
    "京东保": {
        "positive": [
            ("京东保", 20),
            ("京东集团旗下保险经纪平台", 18),
            ("jda", 16),
            ("泰康在线和京东保联合推出", 14),
            ("京东", 6),
        ],
        "negative": [("百万防癌险", 16), ("防癌险", 12), ("恶性肿瘤", 10)],
    },
    "快手": {
        "positive": [
            ("快手app", 14), ("快手logo", 11), ("橙色相机logo", 10),
            ("橙红色相机logo", 9), ("快手", 7),
        ],
        "negative": [("快手极速版", 22), ("闪电角标", 14), ("极速版角标", 16)],
    },
    "快手极速版": {
        "positive": [
            ("快手极速版", 18), ("极速版角标", 14), ("闪电角标", 12),
            ("红橙渐变相机logo", 10), ("快手logo", 4),
        ],
        "negative": [],
    },
    "淘宝联盟": {
        "positive": [
            ("淘宝联盟", 18), ("淘宝logo", 12), ("淘客", 10),
            ("淘宝", 7), ("商品价格", 4), ("购买按钮", 4), ("推广界面", 5),
        ],
        "negative": [],
    },
    "喜番免费短剧": {
        "positive": [
            ("喜番免费短剧", 18), ("喜番", 15), ("免费短剧", 9),
            ("短剧logo", 9), ("短剧", 5),
        ],
        "negative": [],
    },
    "红果短剧": {
        "positive": [
            ("红果短剧", 18), ("红果", 15), ("该故事纯属虚构", 8),
        ],
        "negative": [],
    },
    "闲鱼": {
        "positive": [
            ("闲鱼", 18), ("闲鱼app", 18), ("闲鱼logo", 12), ("闲鱼小法庭", 10),
        ],
        "negative": [],
    },
    "今日头条": {
        "positive": [
            ("今日头条", 18), ("今日头条logo", 12), ("头条app", 16),
        ],
        "negative": [],
    },
    "中国移动APP-签到有礼+10元话费": {
        "positive": [
            ("中国移动logo", 16), ("中国移动", 12), ("签到有礼", 10),
            ("签到领话费", 10), ("10元话费", 9), ("至高10元话费", 10),
        ],
        "negative": [],
    },
}

LEGACY_DIRECTION_RULES: dict[str, list[tuple[str, int]]] = {
    "插画": [
        ("卡通插画", 12), ("插画场景", 10), ("绘制人物", 8), ("卡通人物", 7),
    ],
    "盾牌": [("大型盾牌", 14), ("盾牌主导", 14), ("盾牌", 10), ("锁形盾牌", 9)],
    "上下左右图文": [
        ("多区块图文", 14), ("上下左右分区", 14), ("四区块", 10), ("组合信息区块", 9),
    ],
    "图标": [
        ("权益图标", 13), ("功能图标", 12), ("图标主导", 12), ("多个图标", 9),
    ],
    "小程序": [
        ("小程序卡片界面", 15), ("小程序界面", 13), ("头像卡片", 10),
        ("卡片界面", 7), ("登录界面", 5),
    ],
    "诱导类": [
        ("登录按钮主导", 16), ("一键登录", 14), ("立即登录", 12),
        ("领取按钮主导", 12), ("极简按钮界面", 10),
    ],
    "月亮": [
        ("发光月亮", 15), ("圆月", 13), ("月亮主导", 14), ("月亮", 10), ("夜景", 4),
    ],
    "自动衍生": [
        ("qq阅读二次组合", 18), ("嵌套广告截图", 12), ("原素材面板", 12),
        ("手机号确认", 10), ("惊喜放送", 9), ("二次组合模板", 8),
    ],
    "大字报": [("大字报", 15), ("大号文字主导", 13), ("文字占比极高", 10)],
    "家庭亲子插画": [
        ("家庭亲子插画", 18), ("亲子插画", 15), ("父母和儿童", 12),
        ("家庭场景", 8), ("亲子", 8), ("儿童", 4),
    ],
    "左右图文": [
        ("左右分栏", 15), ("左文右图", 14), ("左图右文", 14), ("纵向两栏", 10),
    ],
    "老歌": [
        ("经典老歌", 16), ("老歌", 14), ("90年代歌曲", 12),
        ("免费听歌", 10), ("歌曲搜索", 8),
    ],
    "旅游攻略": [
        ("旅游攻略", 16), ("旅行攻略", 14), ("旅游景点", 11),
        ("地标风景", 10), ("景点", 7), ("旅行", 6),
    ],
    "美女网赚": [
        ("美女网赚", 18), ("女性人物和红包", 15), ("女性人物和现金收益", 15),
        ("美女和金币", 13),
    ],
    "萌宠": [
        ("萌宠", 16), ("宠物主导", 14), ("猫咪", 11), ("狗狗", 11),
        ("猫", 7), ("狗", 7),
    ],
    "网赚": [
        ("现金收益", 13), ("提现", 12), ("赚钱", 10), ("红包", 7),
        ("金币", 6), ("现金红包", 9), ("收益数字", 7),
    ],
    "文字大字报": [
        ("文字大字报", 17), ("大号文字主导", 13), ("文字占比极高", 11),
    ],
    "幸福生活": [
        ("幸福生活", 16), ("生活方式场景", 12), ("休闲生活", 10),
        ("节庆活动", 9), ("日常活动", 8),
    ],
    "一分购": [
        ("一分钱购", 18), ("1分钱购", 18), ("1分购", 17),
        ("一分购", 17), ("0.01元", 15), ("超低价购买", 8),
    ],
    "妙思自动衍生": [
        ("妙思自动衍生", 18), ("房间装饰背景", 12), ("嵌套广告截图", 11),
        ("原广告截图", 11), ("二次组合模板", 9),
    ],
    "短剧": [("短剧", 15), ("影视剧照", 12), ("剧情画面", 11), ("影视人物", 8)],
    "签到有礼": [
        ("签到有礼", 17), ("签到界面", 14), ("签到奖励", 13),
        ("话费奖励", 11), ("签到", 7),
    ],
    "未细分": [],
}

merge_direction_alias_rules(LEGACY_DIRECTION_RULES)

CLASSIFICATION_RULES_SCHEMA_VERSION = 1
PROTECTED_CLASSIFICATION_RULES = (
    ("妙思方图优先", "妙思方图统一进入朋友圈方向"),
    ("完整嵌图优先", "完整内层广告叠加外层场景时进入秒思自动衍生"),
    ("明确语义主体优先", "月亮、人物插画和宠物主体优先于中心位置或图文版式"),
    ("中心单图标优先", "明确中心单图标主体时优先于误报的图文均衡字段"),
    ("插画图标留白冲突", "插画与明确图标证据同时出现且画面大面积留白时，图标方向优先；强制插画必须排除图标和大面积留白"),
    ("大字报背景门槛", "文字主导且背景为风景、纯色、简单渐变或低信息抽象装饰，无竞争主体时进入大字报"),
    ("重叠产品复核", "百度与快手系列产品仍需完整名称、Logo、角标或专属场景交叉验证"),
    ("产品冲突竞争", "医疗险、防癌险及重名 APP 通过正负权重和分差竞争"),
)


def configurable_classification_rules() -> dict[str, Any]:
    """返回界面可编辑的通用权重规则，不暴露程序级优先级实现。"""
    return {
        "schema_version": CLASSIFICATION_RULES_SCHEMA_VERSION,
        "business_line_rules": copy.deepcopy(BUSINESS_LINE_RULES),
        "product_rules": copy.deepcopy(PRODUCT_RULES),
        "direction_rules": copy.deepcopy(DIRECTION_RULES),
    }


def _normalize_weighted_rules(value: Any, path: str) -> list[tuple[str, int]]:
    if not isinstance(value, list):
        raise ValueError(f"{path} 必须是规则数组")
    if len(value) > 300:
        raise ValueError(f"{path} 最多允许 300 条规则")
    output: list[tuple[str, int]] = []
    for index, item in enumerate(value):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(f"{path}[{index}] 必须是 [关键词, 权重]")
        pattern = str(item[0]).strip()
        weight = item[1]
        if not pattern or len(pattern) > 120:
            raise ValueError(f"{path}[{index}] 的关键词不能为空且不能超过 120 字")
        if isinstance(weight, bool) or not isinstance(weight, int) or not 1 <= weight <= 100:
            raise ValueError(f"{path}[{index}] 的权重必须是 1 到 100 的整数")
        output.append((pattern, weight))
    return output


def normalize_classification_rule_overrides(value: Any) -> dict[str, Any]:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError("classification_rules 必须是 JSON 对象")
    allowed_sections = {
        "schema_version", "business_line_rules", "product_rules", "direction_rules",
    }
    unknown_sections = sorted(set(value) - allowed_sections)
    if unknown_sections:
        raise ValueError(f"classification_rules 存在不支持的字段：{', '.join(unknown_sections)}")
    schema_version = value.get("schema_version", CLASSIFICATION_RULES_SCHEMA_VERSION)
    if schema_version != CLASSIFICATION_RULES_SCHEMA_VERSION:
        raise ValueError(f"不支持的规则格式版本：{schema_version}")

    output: dict[str, Any] = {"schema_version": CLASSIFICATION_RULES_SCHEMA_VERSION}
    business_line_rules = value.get("business_line_rules")
    if business_line_rules is not None:
        if not isinstance(business_line_rules, dict):
            raise ValueError("business_line_rules 必须是 JSON 对象")
        normalized_business: dict[str, list[tuple[str, int]]] = {}
        for name, rules in business_line_rules.items():
            if name not in BUSINESS_LINE_RULES:
                raise ValueError(f"不支持新增业务线：{name}")
            normalized_business[name] = _normalize_weighted_rules(
                rules, f"business_line_rules.{name}"
            )
        output["business_line_rules"] = normalized_business

    product_rules = value.get("product_rules")
    if product_rules is not None:
        if not isinstance(product_rules, dict):
            raise ValueError("product_rules 必须是 JSON 对象")
        normalized_products: dict[str, dict[str, list[tuple[str, int]]]] = {}
        for name, groups in product_rules.items():
            if name not in PRODUCT_RULES:
                raise ValueError(f"不支持新增产品：{name}")
            if not isinstance(groups, dict) or set(groups) - {"positive", "negative"}:
                raise ValueError(f"product_rules.{name} 只能包含 positive 和 negative")
            normalized_products[name] = {
                group: _normalize_weighted_rules(
                    groups.get(group, []), f"product_rules.{name}.{group}"
                )
                for group in ("positive", "negative")
            }
        output["product_rules"] = normalized_products

    direction_rules = value.get("direction_rules")
    if direction_rules is not None:
        if not isinstance(direction_rules, dict):
            raise ValueError("direction_rules 必须是 JSON 对象")
        normalized_directions: dict[str, list[tuple[str, int]]] = {}
        for name, rules in direction_rules.items():
            if name not in DIRECTION_RULES:
                raise ValueError(f"不支持新增方向：{name}")
            normalized_directions[name] = _normalize_weighted_rules(
                rules, f"direction_rules.{name}"
            )
        output["direction_rules"] = normalized_directions
    return output


def apply_classification_rule_overrides(value: Any) -> dict[str, Any]:
    normalized = normalize_classification_rule_overrides(value)
    for name, rules in normalized.get("business_line_rules", {}).items():
        BUSINESS_LINE_RULES[name] = list(rules)
    for name, groups in normalized.get("product_rules", {}).items():
        changed_from_primary = groups != PRODUCT_RULES[name]
        PRODUCT_RULES[name] = copy.deepcopy(groups)
        if changed_from_primary and name in LEGACY_PRODUCT_RULES:
            LEGACY_PRODUCT_RULES[name] = copy.deepcopy(groups)
    for name, rules in normalized.get("direction_rules", {}).items():
        changed_from_primary = rules != DIRECTION_RULES[name]
        DIRECTION_RULES[name] = list(rules)
        if changed_from_primary and name in LEGACY_DIRECTION_RULES:
            LEGACY_DIRECTION_RULES[name] = list(rules)
    return normalized


def validate_classification_ui_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("配置必须是 JSON 对象")
    prompt = str(value.get("api_classify_prompt", "")).strip()
    if not prompt:
        raise ValueError("API 分类提示词不能为空")
    if len(prompt) > 80_000:
        raise ValueError("API 分类提示词不能超过 80000 字")
    missing_fields = [
        field
        for field in (*KEYWORD_PROMPT_FIELDS, *STRUCTURED_PROMPT_FIELDS)
        if field not in prompt
    ]
    if missing_fields:
        raise ValueError(f"API 分类提示词缺少必需字段：{', '.join(missing_fields)}")
    rules = normalize_classification_rule_overrides(value.get("classification_rules", {}))
    return {
        "api_classify_prompt": prompt,
        "classification_rules": rules,
    }


def classification_ui_defaults() -> dict[str, Any]:
    return {
        "prompt_version": PROMPT_VERSION,
        "schema_version": CLASSIFICATION_RULES_SCHEMA_VERSION,
        "default_prompt": PROMPT,
        "default_rules": configurable_classification_rules(),
        "required_prompt_fields": [
            *KEYWORD_PROMPT_FIELDS,
            *STRUCTURED_PROMPT_FIELDS,
        ],
        "protected_rules": [
            {"name": name, "description": description}
            for name, description in PROTECTED_CLASSIFICATION_RULES
        ],
    }

_thread_local = threading.local()


def eprint(msg: str) -> None:
    print(f"[API极速分类] {msg}", file=sys.stderr, flush=True)


def emit(line: str) -> None:
    """向 stdout 输出桌面端进度协议行。"""
    print(line, file=sys.stdout, flush=True)


def build_prompt() -> str:
    return """你是广告图片的视觉证据提取器，不是分类器。
你的唯一任务是忠实提取图片中可以直接观察到的关键词，产品和方向会由本地规则系统判断。

## 严格禁止
- 禁止输出 product、direction、category、classification 或任何最终分类结论。
- 禁止根据行业常识猜测看不见的品牌、文字、Logo、角标或场景。
- 禁止把“像某产品”写成事实；不清楚的内容放进 uncertain。

## 提取要求
1. visible_text：逐条抄录清晰可见的关键文字，保持原文；最多 20 条。
2. brand_keywords：只记录可见品牌文字或 Logo 形态，例如：
   “快手极速版”“橙色相机Logo”“闪电角标”“du熊掌Logo”
   “红色极速角标”“百度网盘Logo”“蓝红三角网盘Logo”“中国移动Logo”“淘宝Logo”。最多 8 条。
   - 对名称重叠的产品必须逐字记录完整可见名称，不得把“百度网盘”“百度极速版”简写成
     “百度”，也不得把“快手极速版”简写成“快手”。
   - 如果只看见通用的“百度”“快手”文字或家族 Logo，不要猜普通版或极速版；应继续记录
     可复核的版本证据，例如“极速版角标”“闪电角标”“百度网盘Logo”“网盘文件列表”。
   - 没有看见的版本角标、Logo 或完整产品名禁止补写；不确定时放进 uncertain。
   - 对橙黄色快手 Logo 必须额外观察 Logo 图形右下角：极速版会在相机形主图标右下角带一个
     独立白色小闪电，普通快手没有这个小闪电。只能检查 Logo 自身，画面其他位置的闪电、箭头、
     高光或装饰线均不得作为版本证据。
3. visual_keywords：记录客观主体和视觉元素，例如：
   “大型盾牌”“发光月亮”“权益图标”“卡通插画”“家庭亲子插画”
   “女性人物”“猫咪”“狗狗”“红包”“金币”“旅游景点”“影视剧照”。最多 12 条。
4. layout_keywords：记录客观版式，例如：
   “图标主导”“左右分栏”“多区块图文”“大号文字主导”
   “登录按钮主导”“小程序卡片界面”“嵌套广告截图”“二次组合模板”。最多 12 条。
   - 必须先完成两个客观检查，再补充普通版式词：
      A. 画布层级：layout_keywords 必须且只能记录一个“单层画布”“外层画布+内层完整广告”
         “外层画布+内层素材卡”或“空间层级不确定”。
     B. 第一视觉主体：layout_keywords 必须且只能记录一个“文字第一视觉主体”“插画场景第一视觉主体”
        “中心图标第一视觉主体”“月亮球体第一视觉主体”“宠物第一视觉主体”
        “均衡图文版式”或“第一视觉主体不明确”。这里描述的是肉眼第一眼看到的主体，不是最终分类。
   - 判断第一视觉主体时按占画面积、位置和视觉重量判断，不能因为画面中出现一个锁、盾牌、月亮，
     或出现“左右分栏/多区块图文”，就把辅助元素当成第一主体。
   - 如果外层新画布中嵌入了一张具有自己背景、主体和文字排版的完整广告图，且嵌图之外
     还有新增标题、品牌、人物、产品或装饰元素，必须记录统一关键词“完整原素材嵌入”；
     并同时记录“外层画布+内层完整广告”和“二次组合模板”；可再记录“嵌套广告截图”。
   - 先检查画面的空间层级，再记录图标：如果完整保险广告被放进游戏机/设备屏幕、白色广告卡片、
     展示牌或其他内层画布，且外层还有手、星星、渐变背景、网格、墙面、摇杆、挂牌或装饰场景，
     必须记录“完整原素材嵌入”和“二次组合模板”；内层广告中的锁、箭头或盾牌不能改变这个结论。
   - 复古机器、街机、售货机或老式终端承载保险屏幕/多块文字面板，外层还能看到墙面、摇杆、
     电话线、机身或其他设备结构时，同样属于外层设备+内层完整广告；必须记录“外层画布+内层完整广告”
     “完整原素材嵌入”“二次组合模板”，不能因为机器是画面中心而写成“单层画布”或中心图标。
    - “完整原素材嵌入”要求内层同时具有自己的广告文字/品牌/权益信息和独立画面背景；例如设备屏幕
       中的一整张保险广告、外层手部/星星包围的白色保险广告卡、渐变或网格背景上叠放的完整保险广告卡，
       都属于嵌图。只看见一个独立锁图标、一个普通卡片或一条边框时不要记录嵌图关键词。
    - 内层不必是一张信息完整的广告才算衍生：如果一个有明确边界的圆角卡片/面板内部拥有自己的背景、
      主视觉和一句独立标题/说明，而卡片边界外又新增品牌 Logo、产品标题、五星评分、口号、CTA，或出现
      明显不同的第二套延展背景，这属于“内层素材卡被外层再次排版”。必须记录“外层画布+内层素材卡”
      “素材卡嵌入”“二次组合模板”，并令 embedded_media_panel=true、outer_relayout=true；即使
      embedded_ad_complete=false，也不能写成单层中心图标。
    - 典型竖版示例：上方白色圆角卡内是独立图案/照片和一句说明，卡外下方另有品牌、产品名、五星评分
      与宣传口号，上下区域还使用渐变背景和网格背景。这是内层素材卡+外层营销排版，不是普通的
      “上方图标+下方文字”。
   - 以下也属于双层画布，不能只写“左右分栏/上下分栏”：左侧或上方是一张带自身品牌、主视觉、
     权益和免责声明的完整保险广告，右侧或下方又新增品牌口号、人物头像、五星评分或大面积延展背景；
     以及把完整保险广告贴到画架、展示牌、节庆边框、红色折扇烟花背景中的画面。此时必须记录
     “外层画布+内层完整广告”“完整原素材嵌入”“二次组合模板”。
   - 特别检查竖版上下结构：如果上方圆角矩形区域内部同时含有图标、保险权益/费率和免责声明，
     下方又出现卡片外的品牌、主标题、卖点或行动文案，这不是“上方图标+下方文字”的单层海报，
     而是“上方完整保险广告+下方外层二次排版”。即使内外背景同色、锁图标最醒目，也必须记录
     “外层画布+内层完整广告”“完整原素材嵌入”“二次组合模板”，不能写“单层画布”。
   - 完整保险广告被做成独立票券/优惠券物件时也属于双层画布：票券自身具有圆角或锯齿边、星级评分、
     条形码/票码、主视觉、权益费率、免责声明和大标题中的多项，同时票券四周还能看到独立的灰色、纯色
     或渐变外层留白。此时外层即使没有新增人物或文案，也已经发生“完整广告票券化后置于新画布”的
     二次排版；必须记录“外层画布+内层完整广告”“完整原素材嵌入”“二次组合模板”。票券内部居中的
     齿轮、锁或盾牌不能把整张图写成单层中心图标。
    - 只有单个锁、盾牌等图标直接绘制在当前画布上，不属于嵌图；仅有矩形卡片、边框、
       手机屏幕或竖版比例，也不得记录“完整原素材嵌入”或“嵌套广告截图”。普通宠物/人物照片与文字
       在同一张圆角卡内连续排版，卡外没有新增品牌、评分、标题、口号或第二套背景时，也属于单层画布，
       embedded_media_panel=false、outer_relayout=false。
   - 插画、月亮、图标和文字发生竞争时必须看主体：人物/家庭/完整卡通场景占主画面时记录
     “插画场景第一视觉主体”，即使背景有满月或底部有权益列表；球体、行星、月亮或同心圆天体占据
     中心且人物场景不占主导时记录“月亮球体第一视觉主体”，不要写成“中心图标第一视觉主体”；
     大型球体同时带有圆环、轨道、同心圆、星环或多个圆盘/月相构图时，应按星球或天体理解，
     即使表面没有清晰月面纹理也记录“月亮球体第一视觉主体”，不能仅因居中而写成图标；
     如果弯月、月牙、星星只出现在背景或作为小装饰，而文字、聊天气泡、手机、人物、盾牌或多区块信息
     才是第一视觉主体，则不得记录“月亮球体第一视觉主体”，graphic_type 也不得写“月亮/天体”；
     风景照片只是底图而大标题最醒目时记录“文字第一视觉主体”；只有文字区和图像区视觉重量接近、
     各自承担并列信息时才记录“均衡图文版式”。
   - “插画场景第一视觉主体”必须有卡通、手绘、水彩等绘制媒介证据。具有写实皮肤、头发、服装纹理、
     真实光影或摄影景深的人物画面属于人物照片，即使由 AI 生成也不得记录“插画场景第一视觉主体”；
     写实人物照片与文字左右或上下并列时，应如实记录“均衡图文版式”和对应的左右/上下结构。
5. 必须另外输出以下结构化视觉字段，它们描述整张画面的关系，不是最终分类：
   - graphic_type：只能是“图标”“月亮/天体”“插画”“人物照片”“风景照片”“产品照片”“其他”或“不明确”。
     画面里出现盾牌、云朵、漏斗、剪贴板等图标，只说明 graphic_type=“图标”，不代表整张图由图案主导。
     graphic_type 必须描述第一视觉主体，不能被周围的装饰小图标带偏；满月、月球、行星、月相或
     带圆环/轨道/同心圆结构的中心球体是第一主体时必须写“月亮/天体”，即使没有清晰月面纹理，
     或周围存在钻石、权益及按钮小图标。
   - composition_relation：只能是“文字主导”“图案主导”“图文均衡”或“不明确”。文字区和图案区
     各约占 35%～65%，或双方都承担主要信息时必须写“图文均衡”。
   - composition_structure：只能是“左右图文”“上下图文”“中心单主体”“多区块图文”“文字覆盖风景”
     “其他”或“不明确”。左侧有标题、两条以上权益/价格/按钮，右侧有独立 3D 图案时属于“左右图文”；
     上方大标题、下方主要图案且视觉重量接近时属于“上下图文”。
   - background_type：只能是“纯自然风景”“城市风景”“纯色/简单抽象”“带人物风景”“插画场景”
     “抽象渐变”“室内场景”“其他”或“不明确”。森林、山川、湖海、蓝天、草原等全幅真实风景且没有人物、动物、产品或 3D 主体时，
     写“纯自然风景”；城市天际线、城市建筑全景、海滨城市或海湾城市全景作为全幅背景且没有竞争主体时，
     写“城市风景”；纯色、简单渐变，或只有低信息量圆形/几何色块、纸张/文字承载框等装饰时，
     写“纯色/简单抽象”。
   - text_overlay_on_scenery：布尔值。文字直接叠在全幅真实风景照片上时为 true。
   - competing_visual_subject：布尔值。存在盾牌、3D 图标、人物、动物、产品、插画、权益卡片或数据图表等
     与文字竞争的独立视觉主体时为 true；只有文字框、描边或承载文字的色块时为 false。
    - canvas_layer：只能是“单层画布”“外层画布+内层完整广告”“外层画布+内层素材卡”或
      “空间层级不确定”，必须与
      layout_keywords 的画布层级结论一致。
    - embedded_ad_complete：布尔值。内层矩形/屏幕/展示牌本身同时具有独立背景、主视觉和完整广告信息
      （品牌、权益/费率、免责声明中至少两类）时为 true；只有单个图标或普通文字卡片时为 false。
    - embedded_media_panel：布尔值。有明确边界的内层卡片/面板拥有自己的背景、主视觉和独立标题/说明，
      且边界外仍存在另一套品牌、标题、评分、口号、CTA 或延展背景时为 true；它不要求内层具备权益、
      费率或免责声明。普通单层图标卡、宠物照片卡或人物照片卡没有独立外层再排版时为 false。
    - outer_relayout：布尔值。内层广告边界之外另有新增品牌、标题、人物、产品、卖点、行动文案或装饰场景
     时为 true；完整广告被改造成带锯齿边/条形码等边界的独立票券物件并置于外层留白画布时也为 true；
     所有内容都属于同一张连续画布时为 false。
   - 大号文字与盾牌/云朵/漏斗/剪贴板等图案同时醒目，或者左右/上下双方各自承担主要信息时，
     composition_relation 必须是“图文均衡”，不能因为图案本身是图标就写“图案主导”。
   - 反例：画面大面积留白，只有一个居中的锁、灯泡、盾牌等单一图标，下面只有一句口号和一行简短
     权益/价格，即使文字位于图标下方，也必须写 graphic_type=“图标”、composition_relation=“图案主导”、
     composition_structure=“中心单主体”；不能写“图文均衡”或“上下图文”。只有上下两区都形成独立、
     视觉重量接近的信息面板时，才是“上下图文”。
   - 字段冲突时按以下顺序处理：graphic_type=“图标”且 composition_structure=“中心单主体”优先于
     composition_relation=“图文均衡”；“中心图标第一视觉主体”且没有左右分栏、文字第一视觉主体时，
     也优先视为中心单图标。底部有权益条、信息卡片或多行短文案不能改变中心单主体。
   - 语义主体优先于位置：明确的“月亮球体第一视觉主体”“插画场景第一视觉主体”“宠物第一视觉主体”
     优先于中心单主体/中心图标；月亮、人物插画或宠物不能仅因居中而写成图标。
   - 保险竖版闪屏若有中心圆盘/透明球体，外围约八个环绕圆形节点形成九宫格或环形阵列，背景又是
     夜空、山水、湖面或宇宙场景，必须在 layout_keywords 中记录“九宫格环绕”“八个圆点按钮”
     和“同心圆盘”等实际可见构图。中心圆盘内的钻石、盾牌等物体可以另行记录，但不能导致模型
     漏掉整套月相/天体模板；中心本身具有月面、球体或行星质感时，graphic_type 必须写“月亮/天体”。
   - “文字主导”不能单独代表大字报；还必须是全幅自然/城市风景、纯色、简单渐变或低信息量抽象装饰背景，
     并且没有人物、产品、图标、插画、数据图表等竞争主体。文字框、按钮、描边、纸张承载框和简单几何色块不算竞争主体。
   - 禁止输出“中心主体+多元素信息场景”这个笼统版式词。必须改用 composition_relation 和
     composition_structure 描述真实关系；中心有主体、周围有装饰元素本身不能决定任何方向。
   - 当画面存在大面积空白/浅色留白、主体周围明显没有信息或装饰时，必须在 visual_keywords 或
     layout_keywords 中明确记录“大面积留白”或“大面积浅色留白”；不能只写“纯色背景”。
   - 当同一画面同时出现插画/卡通主体和明确的盾牌、锁、云朵、漏斗、剪贴板、数据图表等图标关键词时，
     两类证据都要保留，不能因为人物或家庭场景存在就删除图标证据。
6. action_keywords：记录价格、收益和操作证据，例如：
   “一分钱购”“提现”“现金收益”“签到奖励”“立即登录”“领取按钮”。最多 10 条。
7. scene_keywords：记录场景，例如：
   “家庭场景”“生活方式场景”“房间装饰背景”“剧情画面”“地标风景”。最多 8 条。
8. confidence：仅表示上述证据提取的整体清晰度，范围 0 到 1，不代表分类置信度。
9. uncertain：记录模糊、被遮挡或存在多种解释的内容；没有则返回空数组。
10. kuaishou_logo_variant：只能是“右下角带小闪电”“不带小闪电”
   “未识别到橙黄色快手Logo”或“不确定”。即使图片没有任何快手产品文字，只要橙黄色 Logo 清楚，
   也必须检查右下角的小闪电；Logo 太小、被裁切、遮挡或颜色形态不清时写“不确定”。

关键词应短、客观、可复查。颜色不能单独当作品牌，必须同时描述形状或角标。
只返回一个合法 JSON 对象，不要 Markdown，不要解释：
{"visible_text":["图片中可见原文"],"brand_keywords":["可见品牌或Logo特征"],
"visual_keywords":["客观视觉元素"],"layout_keywords":["客观版式"],
"action_keywords":["价格收益或操作"],"scene_keywords":["客观场景"],
"graphic_type":"不明确","composition_relation":"不明确",
"composition_structure":"不明确","background_type":"不明确",
"text_overlay_on_scenery":false,"competing_visual_subject":false,
 "canvas_layer":"空间层级不确定","embedded_ad_complete":false,"embedded_media_panel":false,
 "outer_relayout":false,"kuaishou_logo_variant":"未识别到橙黄色快手Logo",
 "confidence":0.0,"uncertain":["不确定证据"]}
"""


PROMPT = build_prompt()

PROMPT_POLICY_ADDENDUM = """
## V8.1 视觉口径补充
- 纯色、简单渐变或只有低信息量几何/纸张装饰的背景，如果文字是第一视觉主体且没有独立竞争主体，
  必须如实输出 background_type="纯色/简单抽象"、composition_relation="文字主导"。
- 大型球体同时具有圆环、轨道、同心圆、星环或多个圆盘/月相构图时，按星球或天体提取：
  graphic_type="月亮/天体"，第一视觉主体写“月亮球体第一视觉主体”，不能写成中心图标。
- 保险竖版闪屏出现中心圆盘/透明球体、八个环绕圆形节点和夜空/山水/湖面/宇宙背景时，必须记录
  “九宫格环绕”“八个圆点按钮”“同心圆盘”及对应夜景。中心内即使是钻石或盾牌，也不能漏掉整体构图；
  中心具有月面、球体或行星质感时必须写 graphic_type="月亮/天体"。
- 弯月、月牙和星星仅作背景装饰时，不能据此输出月亮/天体；必须服从文字、聊天、手机、人物、
  盾牌或多区块信息等真正的第一视觉主体。
- 复古机器、街机、售货机或老式终端承载完整保险屏幕/多块文字面板，且外层存在墙面、摇杆、
  电话线或机身结构时，必须按外层设备+内层完整广告提取三项嵌图标记，不能写成单层画布。
- 竖版画面上方圆角卡片内同时有锁/盾牌、权益费率和免责声明，下方卡片外又有品牌、标题或行动文案时，
  必须输出 canvas_layer="外层画布+内层完整广告"、embedded_ad_complete=true、outer_relayout=true；
  卡片内部的中心锁不能把整张图改写为单层中心图标。
- 完整保险广告具有票券锯齿边、星级评分、条形码/票码、权益费率、免责声明和大标题，并作为独立票券
  放在灰色/纯色/渐变外层留白中时，即使外层没有新增文案，也必须输出
  canvas_layer="外层画布+内层完整广告"、embedded_ad_complete=true、outer_relayout=true。
- 上方有边界的圆角素材卡自身包含背景、主视觉和一句标题/说明，卡外下方又新增品牌、产品标题、五星评分、
  宣传口号或第二套背景时，即使内层没有权益/费率/免责声明，也必须输出
  canvas_layer="外层画布+内层素材卡"、embedded_media_panel=true、outer_relayout=true，
  embedded_ad_complete 可以为 false。普通宠物/人物照片卡没有卡外二次排版时仍是单层画布。
- 返回 JSON 顶层必须始终包含以下四个字段，不得省略：
  "canvas_layer":"单层画布|外层画布+内层完整广告|外层画布+内层素材卡|空间层级不确定",
  "embedded_ad_complete":true或false,"embedded_media_panel":true或false,
  "outer_relayout":true或false。
""".strip()

KUAISHOU_LOGO_POLICY_ADDENDUM = """
## V8.3 快手 Logo 版本证据（先看颜色，再看小闪电）
- 返回 JSON 顶层必须同时包含 kuaishou_logo_style 与 kuaishou_logo_variant 两个字段。
- kuaishou_logo_style 只能是“纯橙色无渐变”“橙色渐变”“未识别到橙黄色快手Logo”或“不确定”：
  * 图标是单一平铺的橙色/橙红色块，整块颜色由内到外没有深浅变化 → 写“纯橙色无渐变”，这是普通快手。
  * 图标内部有明显渐变（红→橙、橙→粉，或左上与右下颜色明显不同） → 写“橙色渐变”，这是快手极速版。
  * 只要颜色看得清，就必须在这两者中选一个；只有完全无法分辨颜色时才写“不确定”。
- kuaishou_logo_variant 只能是“右下角带小闪电”“不带小闪电”
  “未识别到橙黄色快手Logo”或“不确定”，描述 Logo 图形自身右下角是否有与相机图形分离的白色小闪电。
- 该 Logo 通常很小（常见 30~70 像素）。判断版本时**以颜色为主、小闪电为辅**：
  颜色判出“橙色渐变”即为快手极速版，判出“纯橙色无渐变”即为普通快手；小闪电只作辅助佐证。
- 只有确实看到与相机图形分离的白色小闪电时才写“右下角带小闪电”；看不清必须写“不确定”，
  **禁止为了让结论完整而猜测**。广告画面其他位置的闪电、箭头、高光、星芒或装饰线不能当作 Logo 小闪电。
- Logo 被遮挡、被裁切，或无法确认橙黄色相机形轮廓时，两个字段都写“不确定”。
- 必须区分“橙色圆角方块 + 白色播放三角”与快手的“相机”形 Logo 这两种不同图形：
  快手图标内部是**相机**轮廓（圆角矩形机身 + 同心圆镜头）；
  红果短剧等短剧 App 的图标内部是**播放三角**。
  若图标内部是播放三角，brand_keywords 必须写成“橙色圆角方块播放三角Logo”，
  **不能写成“橙色相机Logo”**——两者形状不同，不要混为一谈。
""".strip()


UI_MIMICRY_POLICY_ADDENDUM = """
## V8.4 界面伪装元素（违规素材识别用）
- 返回 JSON 顶层必须包含 ui_mimicry_elements，值是数组；没有时写 []。
- 只登记**伪装成系统或 App 界面控件、看起来能被点击**的元素，且只能取这些字面值：
  "播放按钮"、"关闭按钮"、"全屏按钮"、"静音按钮"、"音量条"、"进度条"、
  "返回键"、"系统弹窗"、"假搜索框"、"假输入框"、"系统状态栏"、"系统导航栏"。
- 判断标准：该元素**不属于广告内容本身**，而是伪装成播放器 / 浏览器 / 手机系统的控件，
  用来让用户误以为可以点击。例如：画面中央或角落的大号播放三角、角落的 ×、模拟视频
  进度条、模拟“内存不足 / 权限申请 / 立即清理 / 系统升级”的弹窗、模拟手机顶部状态栏
  或底部导航栏。
- 下列情况**必须写 []，不得登记**（它们是正常素材内容，不是伪装控件）：
  * 品牌 Logo 内部的图形 —— 例如红果短剧、腾讯动漫图标内部的白色播放三角，属品牌标识；
  * 权益 / 卖点列表项前面作为项目符号的小圆图标、盾牌、勾选标记；
  * 纯装饰性圆点或圆形按钮 —— 例如“八个圆点按钮”“蓝色圆形按钮”“发光圆点”；
  * 商品图上描述功能的文字 —— 例如“静音滑轮”“音量调节”这类卖点文案；
  * 广告自身的行动按钮 —— “立即购买”“立即下载”“点击查看”等属广告内容。
- 只有确实看到上述伪装控件时才登记；看不清或不确定就写 []，**禁止猜测**。
""".strip()


PROHIBITED_POLICY_ADDENDUM = """
## V8.5 违禁元素（违规素材识别用）
- 返回 JSON 顶层必须包含 prohibited_elements，值是数组；没有时写 []。
- 这是**广告发布前的合规审查**场景：需要客观、如实地记录画面中实际出现的内容，
  供人工复核与拦截。只做事实记录，不发表评价、不做延伸解读、不上价值判断。
- 只登记画面里确实出现过的下列元素，且只能取这些字面值：
  涉政类："国旗"、"国徽"、"党徽"、"国家领导人形象"、"人民币图案"、"军旗军徽"、"国家机关标识"；
  涉黄类："色情裸露"、"性暗示"、"低俗挑逗"。
- **插画、卡通、动漫、Q 版、手绘等绘制风格同样要判断**，不能因为“是画的、是卡通的”就跳过。
  经过简化、拟人化、Q 版化处理的国旗、国徽、党徽、国家领导人形象，以及卡通人物的暴露着装、
  挑逗姿态或性暗示场景，都必须记录。这类风格最容易被整体忽略。
- 判断边界（避免误伤，以下都写 []）：
  * 品牌与企业标识 —— 淘宝、快手、QQ阅读、良品铺子等角标或 Logo，即使配色相近也不算；
  * 几何或装饰性图形 —— 只要不是明确可辨认的国家象征，仅形状相似不算；
  * 正常人物着装 —— 连衣裙、运动服、泳装、亲子装等生活场景不算，
    只有明显色情、裸露或性暗示才登记；
  * 普通文字里的“国”“党”等字 —— 如“国漫”“国民好物”，不构成违禁元素。
- 确实看不清或无法判断就写 []，**禁止猜测**；但**看清了就如实记录，不要回避**。
""".strip()


def effective_prompt(config: dict[str, Any]) -> str:
    """仅接受关键词提取协议的自定义提示词，旧版直判提示词自动回退。"""
    custom = str(config.get("api_classify_prompt", "")).strip()
    compatible = custom and all(
        field in custom for field in (*KEYWORD_PROMPT_FIELDS, *STRUCTURED_PROMPT_FIELDS)
    )
    prompt = custom if compatible else PROMPT
    if not all(marker in prompt for marker in (
        "纯色/简单抽象", "大型球体同时", "弯月、月牙和星星仅作背景装饰", "复古机器、街机",
        "八个环绕圆形节点", "embedded_ad_complete", "embedded_media_panel",
    )):
        prompt = f"{prompt.rstrip()}\n\n{PROMPT_POLICY_ADDENDUM}"
    if "kuaishou_logo_variant" not in prompt:
        prompt = f"{prompt.rstrip()}\n\n{KUAISHOU_LOGO_POLICY_ADDENDUM}"
    if UI_MIMICRY_FIELD not in prompt:
        prompt = f"{prompt.rstrip()}\n\n{UI_MIMICRY_POLICY_ADDENDUM}"
    if PROHIBITED_FIELD not in prompt:
        prompt = f"{prompt.rstrip()}\n\n{PROHIBITED_POLICY_ADDENDUM}"
    return prompt


def parse_json_object(text: str) -> dict[str, Any]:
    """兼容代码围栏和模型偶发的前后说明，但只接受一个 JSON 对象。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        cleaned = "\n".join(lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:])
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("API响应中没有完整JSON对象")
    value = json.loads(cleaned[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("API响应不是JSON对象")
    return value


class OutputTruncatedError(RuntimeError):
    """输出被 max_tokens 截断：推理占满预算，正文没生成出来。可加大预算后重试。"""


def extract_response_text(body: dict[str, Any]) -> str:
    """兼容 Anthropic Messages 与常见兼容网关响应。"""
    content = body.get("content", [])
    if isinstance(content, list):
        text = "".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if text.strip():
            return text

    choices = body.get("choices", [])
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message", {})
        content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(content, str) and content.strip():
            return content

    # 没拿到可用文本。先区分"输出预算被推理吃光"——这种情况加大 max_tokens
    # 重试即可，报错要能直接指向解决办法，而不是笼统的"缺少文本内容"。
    usage = body.get("usage", {}) or {}
    if str(body.get("stop_reason") or "") == "max_tokens":
        raise OutputTruncatedError(
            "API输出被 max_tokens 截断（推理占满输出预算，正文未生成；"
            "output_tokens=%s）" % usage.get("output_tokens", "?")
        )
    raise ValueError("API响应缺少文本内容")


def get_session():
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"urllib3 .* doesn't match a supported version",
        )
        import requests

    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        _thread_local.session = session
    return session


def cache_key(image_bytes: bytes, config: dict[str, Any], prompt: str) -> str:
    digest = hashlib.sha256()
    digest.update(PROMPT_VERSION.encode("utf-8"))
    digest.update(str(config.get("model_id", "")).encode("utf-8"))
    digest.update(prompt.encode("utf-8"))
    digest.update(image_bytes)
    return digest.hexdigest()


def read_cache(cache_file: Path) -> dict[str, Any] | None:
    if not cache_file.is_file():
        return None
    try:
        value = json.loads(cache_file.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def write_cache(cache_file: Path, result: dict[str, Any]) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_file.with_suffix(f".{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    temporary.replace(cache_file)


def read_image_dimensions(image_path: Path) -> tuple[int, int]:
    """读取本地图片尺寸；失败时保留 API 分类流程，只是不启用尺寸规则。"""
    try:
        from PIL import Image

        with Image.open(image_path) as image:
            width, height = image.size
        return int(width), int(height)
    except (OSError, ValueError, TypeError, ImportError):
        return 0, 0


ORIENTATION_LANDSCAPE = "横版"
ORIENTATION_PORTRAIT = "竖版"


def image_orientation(image_path: Path) -> str:
    """按真实像素尺寸判定横竖，用于在方向下再分一层目录。

    口径与上游 2026-09-01「方向后按图片真实尺寸追加横版/竖版」一致：
      宽度 ≥ 高度 → 横版；高度 > 宽度 → 竖版。
    方图（宽=高）归入横版，不单列——保持「每个叶子目录的父目录必为横版或竖版」这个不变式。
    读不到尺寸时按横版兜底，保证图片一定有归属、不会因为读不出尺寸而丢件。
    """
    width, height = read_image_dimensions(image_path)
    if width and height and height > width:
        return ORIENTATION_PORTRAIT
    return ORIENTATION_LANDSCAPE


def vision_thinking_param(config: dict[str, Any]) -> dict[str, Any]:
    """推理型模型的推理开关。

    实测（2026-09-21，ai-claude-code-hub + deepseek-v4.1-flash，同一张图同一提示词）：
      不传该字段            ：65.9s，input 4063，output 8449，正文 912 字
      thinking={"type":"disabled"}：20.3s，input 4037，output  397，正文 985 字

    关闭内部推理后输出降到约 1/21、耗时降到 1/3，而正文反而更完整 ——
    说明之前的输出预算基本被推理吃掉，这才是"API响应缺少文本内容"的真正来源。
    output 远低于 max_tokens 之后，截断问题从根上消失。

    用配置项 thinking_mode 覆盖：disabled（默认）/ enabled / off（完全不传该字段）。
    换模型或换网关若报 400，call_vision_api 会自动关掉它重试，不会拖垮整批分类。
    """
    mode = str(config.get("thinking_mode") or "disabled").strip().lower()
    if mode in ("off", "none", "omit"):
        return {}
    if mode in ("enabled", "on"):
        return {"thinking": {"type": "enabled"}}
    return {"thinking": {"type": "disabled"}}


REASONING_EFFORT_VALUES = ("minimal", "low", "medium", "high")


def vision_reasoning_params(config: dict[str, Any]) -> dict[str, Any]:
    """推理相关的请求参数：thinking 开关 + reasoning_effort。

    实测（2026-09-23，ai-claude-code-hub + deepseek-v4.1-flash，妙思素材）：

    1. 该网关**忽略 thinking=disabled**。单张诊断 output_tokens=7642 而正文仅 1241 字，
       约 85% 的输出预算花在推理上；传 disabled 与完全不传该字段耗时几乎相同
       （62.9s vs 65.2s），说明这个开关在这里不起作用。

    2. 砍 max_tokens **不可行**：设 3000 时正文 0 字、JSON 截断——
       推理先把预算烧光，正文一个字都出不来。

    3. reasoning_effort 看似有效但**实测站不住**：
       - 2 张样本：low 40.4s / out 3778，比基线 62.9s / 6240 快 1.56x
       - 8 张样本：low 46.7s，反而比基线 38.7s **更慢**，且
         brand_keywords 从 27 个掉到 18 个（-33%）
       即小样本是噪声。该网关延迟波动很大（同一请求 38.7s~62.9s），
       真正的瓶颈是网关侧排队，不是我们能通过参数控制的输出长度。
       brand_keywords 是产品判定的关键字段，掉三成会直接推高「未知」比例。

    结论：**默认不启用**（留空则不发送该字段）。仅当确认网关侧行为变化后再考虑开启。
    用配置项 reasoning_effort 覆盖，取值 minimal/low/medium/high。
    """
    params = vision_thinking_param(config)
    effort = str(config.get("reasoning_effort") or "").strip().lower()
    if effort in REASONING_EFFORT_VALUES:
        params["reasoning_effort"] = effort
    return params


def call_vision_api(
    image_path: Path,
    config: dict[str, Any],
    cache_dir: Path,
    retries: int = 3,
    refresh_api: bool = False,
) -> dict[str, Any]:
    """仅调用视觉 API；每个线程复用独立 HTTP Session。"""
    if not all(config.get(key) for key in ("api_endpoint", "model_id", "api_key")):
        raise RuntimeError("API配置不完整")

    image_bytes = image_path.read_bytes()
    prompt = effective_prompt(config)
    result_cache = cache_dir / f"{cache_key(image_bytes, config, prompt)}.json"
    if not refresh_api:
        cached = read_cache(result_cache)
        if cached is not None:
            cached["_cached"] = True
            return cached

    media_type = {
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(image_path.suffix.lower(), "image/jpeg")
    encoded = base64.b64encode(image_bytes).decode("ascii")

    base_endpoint = str(config["api_endpoint"]).rstrip("/")
    endpoint = base_endpoint if base_endpoint.endswith("/v1/messages") else f"{base_endpoint}/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": str(config["api_key"]),
        "anthropic-version": "2023-06-01",
    }
    # 输出预算：推理型模型的内部推理同样计入 max_tokens，预算不足时正文一个字都出不来。
    # 允许在配置里用 "max_tokens" 覆盖，否则用 DEFAULT_MAX_TOKENS。
    try:
        max_tokens = int(config.get("max_tokens") or DEFAULT_MAX_TOKENS)
    except (TypeError, ValueError):
        max_tokens = DEFAULT_MAX_TOKENS
    max_tokens = max(256, min(MAX_TOKENS_CEILING, max_tokens))

    started_at = time.perf_counter()
    last_error: Exception | None = None
    reasoning_params = vision_reasoning_params(config)
    for attempt in range(1, retries + 1):
        payload = {
            "model": config["model_id"],
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [{
                "role": "user",
                # 提示词必须排在图片**前面**：网关的 prompt 缓存按前缀匹配，
                # 图片放前面会让每张图的前缀都不同，那 3800 多 token 的提示词
                # 就要每张图重付一次。实测（两张不同的图、同一提示词）：
                #   [图片, 提示词] → input 4445，cache_read 0
                #   [提示词, 图片] → input 1116，cache_read 3328
                # 顺序换来约 75% 的输入 token 节省，耗时也从 15.7s 降到 5.4s。
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": encoded,
                    }},
                ],
            }],
        }
        if reasoning_params:
            payload.update(reasoning_params)
        try:
            response = get_session().post(endpoint, headers=headers, json=payload, timeout=(20, 120))
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = min(12.0, max(1.0, float(retry_after)))
                except ValueError:
                    delay = min(8.0, 1.5 * (2 ** (attempt - 1)))
                raise RuntimeError(
                    f"API HTTP {response.status_code}: {response.text[:240]}",
                    delay,
                )
            if response.status_code != 200:
                # 换网关/换模型后可能不认识这些字段：逐个关掉重试，不要让整批分类瘫掉
                if reasoning_params and response.status_code == 400:
                    rejected_text = response.text.lower()
                    rejected_key = next(
                        (k for k in ("reasoning_effort", "thinking")
                         if k in reasoning_params and k in rejected_text),
                        None,
                    )
                    if rejected_key:
                        reasoning_params.pop(rejected_key, None)
                        eprint(
                            f"{image_path.name} 网关不支持 {rejected_key} 字段，"
                            f"已自动关闭并重试"
                        )
                        continue
                raise RuntimeError(f"API HTTP {response.status_code}: {response.text[:300]}")

            response_body = response.json()
            response_text = extract_response_text(response_body)
            result = parse_json_object(response_text)
            result["_api_raw_text"] = response_text
            result["_api_meta"] = {
                "http_status": response.status_code,
                "endpoint": endpoint,
                "configured_model": str(config.get("model_id", "")),
                "response_model": str(response_body.get("model", "")),
                "response_id": str(response_body.get("id", "")),
                "stop_reason": str(response_body.get("stop_reason", "")),
                "usage": response_body.get("usage", {}),
                "attempt": attempt,
                "elapsed_ms": round((time.perf_counter() - started_at) * 1000),
            }
            write_cache(result_cache, result)
            result["_cached"] = False
            return result
        except Exception as exc:
            last_error = exc
            if isinstance(exc, OutputTruncatedError) and max_tokens < MAX_TOKENS_CEILING:
                # 预算是被推理吃光的，原样重试大概率还是空正文，直接把预算翻倍
                max_tokens = min(MAX_TOKENS_CEILING, max_tokens * 2)
            if attempt >= retries:
                break
            delay = (
                float(exc.args[1])
                if len(exc.args) > 1 and isinstance(exc.args[1], (int, float))
                else min(8.0, 1.5 * (2 ** (attempt - 1)))
            )
            eprint(f"{image_path.name} 第 {attempt} 次调用失败，{delay:.1f}s 后重试：{exc.args[0]}")
            time.sleep(delay)
    raise RuntimeError(f"API连续失败 {retries} 次：{last_error}") from last_error


def to_confidence(value: Any, fallback: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return fallback


def keyword_list(raw: dict[str, Any], field: str, limit: int) -> list[str]:
    value = raw.get(field, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        normalized = text.lower().replace(" ", "")
        if text and normalized not in seen:
            output.append(text[:160])
            seen.add(normalized)
        if len(output) >= limit:
            break
    return output


def structured_choice(raw: dict[str, Any], field: str) -> str:
    value = str(raw.get(field, "")).strip()
    return value if value in STRUCTURED_FIELD_CHOICES[field] else "不明确"


def optional_boolean(raw: dict[str, Any], field: str) -> bool | None:
    value = raw.get(field)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "1", "是"}:
        return True
    if normalized in {"false", "no", "0", "否"}:
        return False
    return None


def normalize_keyword_evidence(raw: dict[str, Any]) -> dict[str, Any]:
    evidence = {
        "visible_text": keyword_list(raw, "visible_text", 20),
        "brand_keywords": keyword_list(raw, "brand_keywords", 8),
        "visual_keywords": keyword_list(raw, "visual_keywords", 12),
        "layout_keywords": [
            value
            for value in keyword_list(raw, "layout_keywords", 12)
            if "".join(value.split()) not in DISABLED_LAYOUT_MARKERS
        ],
        "action_keywords": keyword_list(raw, "action_keywords", 10),
        "scene_keywords": keyword_list(raw, "scene_keywords", 8),
        "uncertain": keyword_list(raw, "uncertain", 8),
        "graphic_type": structured_choice(raw, "graphic_type"),
        "composition_relation": structured_choice(raw, "composition_relation"),
        "composition_structure": structured_choice(raw, "composition_structure"),
        "background_type": structured_choice(raw, "background_type"),
        "text_overlay_on_scenery": optional_boolean(raw, "text_overlay_on_scenery"),
        "competing_visual_subject": optional_boolean(raw, "competing_visual_subject"),
        "canvas_layer": structured_choice(raw, "canvas_layer"),
        "embedded_ad_complete": optional_boolean(raw, "embedded_ad_complete"),
        "embedded_media_panel": optional_boolean(raw, "embedded_media_panel"),
        "outer_relayout": optional_boolean(raw, "outer_relayout"),
        "kuaishou_logo_variant": structured_choice(raw, "kuaishou_logo_variant"),
        "_kuaishou_logo_variant_provided": "kuaishou_logo_variant" in raw,
        "kuaishou_logo_style": structured_choice(raw, "kuaishou_logo_style"),
        "_kuaishou_logo_style_provided": "kuaishou_logo_style" in raw,
        # 界面伪装元素：模型明确判定为「伪装成系统/App 控件」的才算，
        # 旧缓存没有这个字段 → 空列表 → 违规规则不会命中，行为与改动前一致。
        UI_MIMICRY_FIELD: keyword_list(raw, UI_MIMICRY_FIELD, 8),
        # 违禁元素（涉政/涉黄）：同样只在模型明确判定时才有值。
        # 旧缓存没有这个字段 → 空列表 → 违规规则不命中，行为与改动前一致。
        PROHIBITED_FIELD: keyword_list(raw, PROHIBITED_FIELD, 8),
        "evidence_confidence": to_confidence(raw.get("confidence"), 0.0),
        "cached": bool(raw.get("_cached", False)),
    }
    if evidence["kuaishou_logo_variant"] == "不明确":
        evidence["kuaishou_logo_variant"] = "不确定"
    if evidence["kuaishou_logo_style"] == "不明确":
        evidence["kuaishou_logo_style"] = "不确定"
    if not any(evidence[field] for field in KEYWORD_PROMPT_FIELDS[:-1]):
        raise ValueError("API没有返回任何可用关键词证据")
    return evidence


def compact_text(value: str) -> str:
    return "".join(value.lower().split())


FILENAME_PRODUCT_HINTS: tuple[tuple[str, str], ...] = (
    ("百万防癌险", "百万防癌险"),
    ("百万医疗险", "百万医疗险"),
    ("百万医疗", "百万医疗险"),
    ("百度网盘", "百度网盘"),
    ("百度云盘", "百度网盘"),
    ("百度极速版", "百度极速版"),
    ("快手极速版", "快手极速版"),
    ("中国移动", "中国移动APP-签到有礼+10元话费"),
    ("喜番免费短剧", "喜番免费短剧"),
    ("红果短剧", "红果短剧"),
    ("抖音商城", "抖音商城"),
    ("小红书", "小红书"),
    ("联通19元200g", "联通19元200G"),
    ("中国联通", "联通19元200G"),
    ("qq阅读", "QQ阅读"),
    ("闲鱼", "闲鱼"),
    ("今日头条", "今日头条"),
    ("淘宝联盟", "淘宝联盟"),
    ("淘宝", "淘宝联盟"),
    ("快手", "快手"),
    ("百度", "百度"),
)

# 文件名只作为低优先级的补充证据：很多历史素材文件名包含人工方向标签，
# 但不能覆盖 API 已经给出的强视觉证据（例如“月亮”文件名实际为图标）。
FILENAME_DIRECTION_HINTS: tuple[tuple[str, str], ...] = (
    ("家庭亲子插画", "插画"),
    ("阅读权益", "上下左右图文"),
    ("上下左右图文", "上下左右图文"),
    ("文字大字报", "文字大字报"),
    ("大字报", "大字报"),
    ("妙思自动衍生", "妙思自动衍生"),
    ("妙思自动生成", "妙思自动衍生"),
    ("秒思自动衍生", "秒思自动衍生"),
    ("小程序", "小程序"),
    ("一分购", "一分购"),
    ("朋友圈", "朋友圈"),
    ("美女网赚", "美女"),
    ("美女穿搭", "美女"),
    ("美女", "美女"),
    ("旅游攻略", "旅游攻略"),
    ("旅游", "旅游"),
    ("喜番免费短剧", "短剧"),
    ("老歌", "老歌"),
    ("萌宠", "萌宠"),
    ("网赚", "网赚"),
    ("盾牌锁", "图标"),
    ("盾牌", "图标"),
    ("图标", "图标"),
    ("月亮", "月亮"),
    ("插画", "插画"),
    ("美妆", "美妆"),
    ("美食", "美食"),
    ("幸福生活", "幸福生活"),
)

def filename_product_hint(image_name: str) -> str | None:
    corpus = compact_text(image_name)
    for pattern, product in FILENAME_PRODUCT_HINTS:
        if compact_text(pattern) in corpus:
            return product
    return None


# 「命名只能参考」（2026-09-21 用户定调）：文件名是**弱标签**，
# 只在视觉证据沉默（没判出任何产品）时兜底，**不得**在冲突时压倒视觉证据。
# 列在这里的产品，其图标与别的产品极易混淆（红果短剧 = 橙色圆角方块 + 白色播放三角，
# 常被视觉模型读成快手的"相机"形 Logo），但它依然只是兜底候选，不是权威。
# 注意："快手 vs 快手极速版"的版本边界不在这里处理，
# 见 resolve_kuaishou_logo_product()，那边同样是"证据沉默才用文件名"。
FILENAME_FALLBACK_PRODUCTS = {"红果短剧"}


FILENAME_BUSINESS_LINE_HINTS: tuple[tuple[str, str], ...] = (
    ("[保险服务]", "保险"),
    ("[保险]", "保险"),
    ("[小卡]", "小卡"),
    ("[通信服务]", "小卡"),
    ("[卡券]", "卡券"),
    ("[存量]", "存量"),
    ("[APP]", "APP"),
)


def filename_business_line_hint(image_name: str, product: str | None = None) -> str | None:
    corpus = compact_text(image_name)
    for pattern, business_line in FILENAME_BUSINESS_LINE_HINTS:
        if compact_text(pattern) in corpus:
            return business_line
    filename_product = product or filename_product_hint(image_name)
    return PRODUCT_BUSINESS_LINES.get(filename_product or "")


def filename_direction_hints(image_name: str) -> list[str]:
    corpus = compact_text(image_name)
    matches: list[tuple[int, int, str]] = []
    for pattern, direction in FILENAME_DIRECTION_HINTS:
        token = compact_text(pattern)
        position = corpus.find(token)
        if position >= 0:
            matches.append((position, -len(token), direction))
    matches.sort()
    output: list[str] = []
    for _, _, direction in matches:
        if direction not in output:
            output.append(direction)
    return output


def keyword_corpus(evidence: dict[str, Any]) -> str:
    parts: list[str] = []
    for field in KEYWORD_PROMPT_FIELDS[:-1]:
        parts.extend(evidence.get(field, []))
    parts.extend(
        str(evidence.get(field, ""))
        for field in STRUCTURED_PROMPT_FIELDS
        if not isinstance(evidence.get(field), bool) and evidence.get(field) is not None
    )
    return compact_text("|".join(parts))


def calibrated_direction_prediction(
    evidence: dict[str, Any],
) -> tuple[str | None, float, int, list[dict[str, Any]]]:
    """用人工校准样本提炼的关键词概率表复核外部保险方向。

    模型只保存高频客观关键词的计数，不保存图片、文件名或样本 ID；没有
    命中校准词表时返回空，由通用方向规则继续处理。
    """
    model = DIRECTION_CALIBRATION_MODEL
    labels = model.get("labels", [])
    tokens = model.get("tokens", {})
    documents = model.get("documents", [])
    totals = model.get("totals", [])
    if not labels or not tokens or len(documents) != len(labels) or len(totals) != len(labels):
        return None, 0.0, 0, []

    features: dict[str, int] = {}
    for field in model.get("fields", []):
        for value in evidence.get(field, []):
            token = f"{str(field)[0]}={compact_text(str(value))}"
            features[token] = features.get(token, 0) + 1
    matched = sum(count for token, count in features.items() if token in tokens)
    if matched < 1:
        return None, 0.0, 0, []

    alpha = float(model.get("alpha", 0.1))
    vocabulary_size = len(tokens)
    document_total = sum(int(value) for value in documents)
    scores: list[tuple[float, str]] = []
    for index, label in enumerate(labels):
        score = math.log(
            (int(documents[index]) + 1) / (document_total + len(labels))
        )
        denominator = float(totals[index]) + alpha * vocabulary_size
        for token, count in features.items():
            token_counts = tokens.get(token)
            if token_counts is None:
                continue
            score += count * math.log(
                (int(token_counts[index]) + alpha) / denominator
            )
        scores.append((score, str(label)))
    scores.sort(reverse=True)
    margin = scores[0][0] - scores[1][0] if len(scores) > 1 else 0.0
    candidates = [
        {"name": label, "score": round(score, 4)}
        for score, label in scores[:5]
    ]
    direction = None if scores[0][1] == "UNKNOWN" else scores[0][1]
    return direction, margin, matched, candidates


APP_CALIBRATION_LIST_FIELDS = (
    "visible_text",
    "brand_keywords",
    "visual_keywords",
    "layout_keywords",
    "action_keywords",
    "scene_keywords",
)
APP_CALIBRATION_STRUCTURED_FIELDS = (
    "graphic_type",
    "composition_relation",
    "composition_structure",
    "background_type",
)


def calibration_characters(value: Any) -> str:
    return "".join(character for character in compact_text(str(value)) if character.isalnum())


def collection_app_calibration_features(
    evidence: dict[str, Any],
    detected_direction: str = "",
) -> dict[str, int]:
    """把客观证据压缩为不含文件名和样本 ID 的本地校准特征。"""
    features: dict[str, int] = {}
    for field in APP_CALIBRATION_LIST_FIELDS:
        values = evidence.get(field, [])
        if not isinstance(values, list):
            continue
        for value in values:
            normalized = calibration_characters(value)
            if not normalized:
                continue
            fragments = {normalized} if len(normalized) < 2 else {
                normalized[index:index + 2]
                for index in range(len(normalized) - 1)
            }
            for fragment in fragments:
                token = f"{field}:{fragment}"
                features[token] = features.get(token, 0) + 1
    for field in APP_CALIBRATION_STRUCTURED_FIELDS:
        normalized = calibration_characters(evidence.get(field, ""))
        if normalized:
            token = f"{field}={normalized}"
            features[token] = features.get(token, 0) + 1
    normalized_direction = calibration_characters(detected_direction)
    if normalized_direction:
        token = f"detected_direction={normalized_direction}"
        features[token] = features.get(token, 0) + int(
            APP_DIRECTION_CALIBRATION_MODEL.get("detected_direction_weight", 3)
        )
    return features


def calibrated_app_direction_prediction(
    evidence: dict[str, Any],
    detected_direction: str = "",
) -> tuple[str | None, float, float, int, list[dict[str, Any]]]:
    """使用人工确认的 APP 素材证据模型复核方向。"""
    model = APP_DIRECTION_CALIBRATION_MODEL
    labels = model.get("labels", [])
    tokens = model.get("tokens", {})
    documents = model.get("documents", [])
    totals = model.get("totals", [])
    if not labels or not tokens or len(documents) != len(labels) or len(totals) != len(labels):
        return None, 0.0, 0.0, 0, []

    features = collection_app_calibration_features(evidence, detected_direction)
    matched = sum(count for token, count in features.items() if token in tokens)
    if matched < 1:
        return None, 0.0, 0.0, 0, []

    alpha = float(model.get("alpha", 0.3))
    vocabulary_size = len(tokens)
    document_total = sum(int(value) for value in documents)
    scores: list[tuple[float, str]] = []
    for index, label in enumerate(labels):
        score = math.log(
            (int(documents[index]) + 1) / (document_total + len(labels))
        )
        denominator = float(totals[index]) + alpha * vocabulary_size
        for token, count in features.items():
            token_counts = tokens.get(token)
            if token_counts is None:
                continue
            score += count * math.log(
                (int(token_counts[index]) + alpha) / denominator
            )
        scores.append((score, str(label)))
    scores.sort(reverse=True)
    margin = scores[0][0] - scores[1][0] if len(scores) > 1 else 0.0
    top_score = scores[0][0]
    probability_denominator = sum(math.exp(score - top_score) for score, _ in scores)
    probability = 1.0 / probability_denominator if probability_denominator else 0.0
    candidates = [
        {"name": label, "score": round(score, 4)}
        for score, label in scores[:5]
    ]
    direction = None if scores[0][1] in {"UNKNOWN", "未知"} else scores[0][1]
    return direction, probability, margin, matched, candidates


def strong_app_direction(
    evidence: dict[str, Any],
    product: str = "",
) -> tuple[str | None, str]:
    """仅对语义极明确的 APP 内容使用透明强规则。

    返回的名字必须落在 APP 方向清单内（见 APP_DIRECTIONS）：
    业务侧 2026-09-28 定「低价好物 → 购物」、「电商 → 快手电商」，
    这里同步改名，别让强规则产出清单外的方向。
    """
    corpus = keyword_corpus(evidence)
    contains = lambda markers: any(compact_text(marker) in corpus for marker in markers)
    if contains(("0.01元", "0.01元购", "一分钱购", "1分钱购", "1分购", "一分购", "1分起")):
        return "一分购", "明确一分钱购语义"
    if contains(("快手短剧", "星芒短剧", "热播短剧", "免费短剧", "剧集", "第1集", "第2集")):
        return "短剧", "明确短剧或剧集语义"
    if contains(("经典老歌", "经典歌曲", "歌曲推荐", "免费听歌", "青春回忆杀")):
        return "老歌", "明确老歌或听歌语义"
    if contains((
        "摆拍攻略", "拍照攻略", "摄影攻略", "摄影教程", "拍照教程", "拍摄教程",
        "摄影技巧", "拍照技巧", "摆拍技巧", "拍照姿势", "摆拍姿势", "出片技巧", "构图技巧",
    )):
        return "内容创意", "明确摄影、摆拍或出片教程语义"
    if contains(("刷快手奔赴", "刷快手沉浸式")):
        return "官方样式", "明确快手官方口号句式"
    if contains(("低价好物", "好物低价", "全网低价", "超值好物", "9.9包邮")):
        return "购物", "明确低价好物语义"
    if contains(("快手商城", "快手小店", "小店后台", "开店", "入驻", "商家经营", "流量扶持")):
        return "快手电商", "明确电商经营语义"
    return None, ""


def normalize_app_direction_for_product(direction: str, product: str) -> str:
    """历史遗留的「产品未知时快手电商归一为电商」。

    2026-09-28 业务侧定「电商」并入「快手电商」后，这个降级已经没有意义，
    而且会把方向打到清单外（→ 内容创意），所以现在原样返回。
    保留函数签名是为了不破坏既有调用与测试。
    """
    return direction


def resolve_kuaishou_logo_product(
    evidence: dict[str, Any],
    filename_product: str | None = None,
) -> tuple[str | None, str]:
    """按明确快手文案、Logo 颜色或右下角小闪电确定产品版本。

    注意：这个函数会把产品**强制覆盖**成快手/快手极速版，优先级很高。
    判定顺序：画面显式文字 > Logo 颜色 > Logo 小闪电 > 文件名（**仅证据沉默时**）。

    「命名只能参考」（2026-09-21 用户定调）：文件名**不得**在冲突时压倒视觉证据，
    只有在颜色和小闪电都给不出结论、画面里又确实有快手相机形 Logo 时，才用它补齐版本。
    红果短剧等短剧 App 的图标是"橙色圆角方块+白色播放三角"，与快手"相机"形
    Logo 极易混淆，视觉模型常读成"橙色相机Logo"，因此遇到播放三角形状的描述
    直接放弃覆盖，交给常规产品规则。
    """
    visible_text = compact_text("|".join(str(value) for value in evidence.get("visible_text", [])))
    brand_corpus = compact_text(
        "|".join(str(value) for value in evidence.get("brand_keywords", []))
    )
    if filename_product not in {"快手", "快手极速版"} and any(
        marker in brand_corpus
        for marker in ("播放三角", "播放按钮", "播放键", "播放图标", "播放三角形")
    ):
        # 播放三角不是快手的"相机"形 Logo，放弃覆盖
        return None, ""
    if compact_text("快手极速版") in visible_text:
        return "快手极速版", "完整可见文字:快手极速版"
    if any(compact_text(marker) in visible_text for marker in (
        "在快手内即可开店", "在快手内开店", "在快手即可开店", "在快手开店",
        "快手开店", "快手小店", "快手商城", "快手电商",
    )):
        return "快手", "明确可见快手开店、商城或电商经营文案"
    # Logo 颜色是最可靠的版本证据（图标只有 30~70px，小闪电经常看不清）
    logo_style = str(evidence.get("kuaishou_logo_style") or "").strip()
    if logo_style == "橙色渐变":
        return "快手极速版", "Logo为橙色渐变，按快手极速版"
    if logo_style == "纯橙色无渐变":
        return "快手", "Logo为纯橙色无渐变，按普通快手"
    logo_variant = str(evidence.get("kuaishou_logo_variant") or "").strip()
    if logo_variant == "右下角带小闪电":
        return "快手极速版", "橙黄色快手Logo右下角带独立白色小闪电"
    if logo_variant == "不带小闪电":
        return "快手", "橙黄色快手Logo不带右下角小闪电"
    has_kuaishou_camera_logo = (
        any(color in brand_corpus for color in ("橙色", "橙黄色", "橙红色", "红橙"))
        and any(shape in brand_corpus for shape in ("相机", "摄像机"))
        and "logo" in brand_corpus
    )
    if has_kuaishou_camera_logo and filename_product in {"快手", "快手极速版"}:
        # 视觉证据到这里已经沉默（颜色与小闪电都没给出结论）。此时不默认判普通快手
        # ——"没看到小闪电"往往是看不清而不是真没有——改用文件名补齐版本。
        # 这是「命名只能参考」允许的唯一用法：证据沉默时兜底，而非压倒证据。
        return filename_product, f"Logo版本证据不足，按文件名兜底：{filename_product}"
    return None, ""


DERIVED_COMPLETE_MARKERS: tuple[str, ...] = (
    "完整原素材嵌入",
    "完整原图嵌入",
    "完整广告图被嵌入",
    "嵌入完整广告图",
    "内嵌完整广告图",
    "内嵌广告画面",
    "画中画广告",
    "画中画原素材",
)
DERIVED_NESTED_MARKERS: tuple[str, ...] = (
    "嵌套广告截图",
    "原广告截图",
    "原素材面板",
)
DERIVED_COMPOSITE_MARKERS: tuple[str, ...] = (
    "二次组合模板",
    "外层新增营销元素",
    "外层装饰场景",
)
DERIVED_LAYER_MARKERS: tuple[str, ...] = (
    "外层画布+内层完整广告",
)
DERIVED_OUTER_SCENE_MARKERS: tuple[str, ...] = (
    "复古游戏机造型",
    "游戏机造型",
    "金色小猪存钱罐",
    "小猪存钱罐",
    "摄像机监视器",
    "监视器画面",
    "房间装饰背景",
    "桌面文具",
    "彩色铅笔",
    "家庭野餐插画",
    "手持喇叭插画",
    "手持扩音器插画",
    "粉色装订线条",
)


def derived_nested_hint(corpus: str) -> bool:
    """识别任意产品中把一张完整原素材再次嵌入外层画布的衍生图。

    这里故意不把“卡片界面”“矩形边框”“单个图标”算作嵌图，避免竖版
    锁图标和普通海报卡片被误分到“秒思自动衍生”。
    """
    contains = lambda markers: any(compact_text(marker) in corpus for marker in markers)
    complete = contains(DERIVED_COMPLETE_MARKERS)
    nested = contains(DERIVED_NESTED_MARKERS)
    composite = contains(DERIVED_COMPOSITE_MARKERS)
    outer_scene = contains(DERIVED_OUTER_SCENE_MARKERS)

    if contains(DERIVED_LAYER_MARKERS):
        return True

    # 完整内层广告和明显外层场景必须同时成立。视觉模型偶尔会把普通汽车
    # 海报、手机界面或单一卡片误写成“嵌套广告截图”，不能仅凭该词强制归类。
    if composite and (complete or nested):
        return True
    if complete and nested and outer_scene:
        return True

    # 少量人工校准样本中模型漏写了“嵌套”，但外层物体与内层广告载体的
    # 组合非常明确，可由两个互相独立的视觉对象交叉确认。
    pig_template = contains(("金色小猪存钱罐", "小猪存钱罐")) and contains(
        ("红色卡片", "橙色卡片", "卡片界面", "锁图标", "锁形图标", "盾牌图标")
    )
    monitor_template = contains(("摄像机监视器", "监视器画面")) and contains(
        ("锁图标", "锁形图标", "圆形进度环")
    )
    decorated_template = contains(("手持喇叭插画", "手持扩音器插画")) and contains(
        ("锁图标", "盾牌挂锁图标", "装饰边框", "黄色圆点背景")
    )
    insurance_copy = contains((
        "保险", "医疗险", "防癌险", "投保", "保障", "保额", "赔付", "免赔额", "理赔",
    ))
    physical_screen_template = (
        contains((
            "复古游戏机造型", "游戏机造型", "复古米色机器", "复古机器",
            "街机", "售货机", "老式终端", "机器设备",
        ))
        and contains((
            "屏幕+挂牌组合", "完整屏幕广告", "机器设备承载广告文字",
            "设备承载广告文字", "上下多面板结构", "保险屏幕",
        ))
        and insurance_copy
    )
    card_outer_groups = (
        ("卡通手", "手部", "双手"),
        ("星星装饰", "星星", "彩色星星"),
        (
            "渐变背景", "渐变蓝色背景", "蓝色渐变背景", "浅蓝色渐变背景",
            "浅蓝渐变背景", "蓝白渐变背景", "红色渐变背景", "紫色渐变背景",
        ),
        ("红色墙面背景", "复古街机场景"),
        ("六边形网格背景",),
        (
            "虚点圆环", "虚线圆环", "虚线圆弧", "虚线圆圈", "同心圆光环",
            "蓝色同心圆", "蓝色同心圆光环", "圆形光环",
        ),
        ("螺旋线条装饰", "螺旋电话线"),
    )
    card_outer_count = sum(
        any(compact_text(marker) in corpus for marker in group)
        for group in card_outer_groups
    )
    card_like = contains((
        "白色卡片", "白色面板", "白色屏幕", "白色广告卡片", "白色广告面板", "卡片式界面",
        "白色圆角卡片", "圆角白色卡片", "白色圆角矩形卡片", "圆角卡片容器",
    ))
    card_layout = contains((
        "卡片式界面", "大号文字主导", "图标主导", "上下分栏", "上下结构", "多区块图文",
    ))
    decorated_ad_card = insurance_copy and card_like and card_layout and card_outer_count >= 3
    return pig_template or monitor_template or decorated_template or physical_screen_template or decorated_ad_card


def has_ticket_ad_artifact_evidence(evidence: dict[str, Any]) -> bool:
    """识别被票券化后放置到新画布的完整保险广告。"""
    visual_corpus = compact_text("|".join([
        *(str(value) for value in evidence.get("visual_keywords", [])),
        *(str(value) for value in evidence.get("scene_keywords", [])),
    ]))
    text_corpus = compact_text("|".join(
        str(value) for value in evidence.get("visible_text", [])
    ))
    contains = lambda corpus, markers: any(compact_text(marker) in corpus for marker in markers)
    card_carrier = contains(visual_corpus, (
        "白色广告卡片", "白色圆角卡片", "圆角白色卡片", "白色票券", "保险票券",
    ))
    ticket_edge = contains(visual_corpus, (
        "票券锯齿底边", "票券锯齿边", "锯齿票券边缘", "优惠券锯齿边", "票券边界",
    ))
    ticket_code_or_rating = contains(visual_corpus, (
        "条形码", "票码", "条纹元素", "星级评分", "五星评分", "金色五星",
    ))
    outer_canvas = contains(visual_corpus, (
        "灰色渐变外底", "渐变外底", "外层灰色背景", "外层纯色背景", "外层渐变背景",
        "灰色外层留白", "外层留白",
    ))
    complete_insurance_copy = contains(text_corpus, (
        "投保须知", "保险条款", "具体费率", "保险金额", "责任范围", "承保保险公司",
    ))
    dense_marketing_content = (
        len(evidence.get("visible_text", [])) >= 5
        and len(evidence.get("action_keywords", [])) >= 2
    )
    return bool(
        card_carrier
        and ticket_edge
        and ticket_code_or_rating
        and outer_canvas
        and complete_insurance_copy
        and dense_marketing_content
    )


def has_embedded_media_panel_evidence(evidence: dict[str, Any]) -> bool:
    """识别不含完整广告声明、但被外层再次排版的独立素材卡。"""
    if evidence.get("canvas_layer") == "外层画布+内层素材卡":
        return True
    if evidence.get("embedded_media_panel") is True and evidence.get("outer_relayout") is True:
        return True

    # 兼容旧审计：r6 可能把这类素材写成单层画布。兜底要求内卡、上下区域、
    # 五星评分、两套明确背景和外层品牌文案同时成立，避免普通圆角照片卡误伤。
    visual_corpus = compact_text("|".join([*map(str, evidence.get("visual_keywords", [])),
                                           *map(str, evidence.get("scene_keywords", []))]))
    layout_corpus = compact_text("|".join(map(str, evidence.get("layout_keywords", []))))
    contains = lambda corpus, markers: any(compact_text(marker) in corpus for marker in markers)
    card_carrier = contains(visual_corpus, (
        "白色圆角卡片", "圆角白色卡片", "白色圆角矩形卡片", "圆角卡片容器", "圆角卡片",
    ))
    upper_lower_layout = contains(layout_corpus, (
        "上大图下大字", "上方图片下方文字", "上方素材卡下方文字", "上方卡片下方文字", "上下图文",
    ))
    outer_rating = contains(visual_corpus, (
        "五颗黄色星星", "五颗黄星", "五星评分", "五颗星星", "金色五星", "星级评分",
    ))
    upper_background = contains(visual_corpus, (
        "蓝色渐变上部背景", "渐变上部背景", "上部渐变背景", "上半部渐变背景",
    ))
    lower_background = contains(visual_corpus, (
        "白色立方体网格下部背景", "立方体网格下部背景", "下部网格背景", "下半部网格背景",
    ))
    outer_identity = (
        len(evidence.get("brand_keywords", [])) >= 1
        and len(evidence.get("visible_text", [])) >= 4
    )
    return bool(
        card_carrier
        and upper_lower_layout
        and outer_rating
        and upper_background
        and lower_background
        and outer_identity
    )


def has_derived_nested_evidence(evidence: dict[str, Any]) -> bool:
    """结合新结构字段与旧关键词，识别完整广告被再次排版的双层画布。"""
    if derived_nested_hint(keyword_corpus(evidence)):
        return True
    if evidence.get("canvas_layer") == "外层画布+内层完整广告":
        return True
    if evidence.get("embedded_ad_complete") is True and evidence.get("outer_relayout") is True:
        return True
    if has_embedded_media_panel_evidence(evidence):
        return True
    if has_ticket_ad_artifact_evidence(evidence):
        return True

    layout_corpus = compact_text("|".join(
        str(value) for value in evidence.get("layout_keywords", [])
    ))
    visual_corpus = compact_text("|".join([
        *(str(value) for value in evidence.get("visual_keywords", [])),
        *(str(value) for value in evidence.get("scene_keywords", [])),
    ]))
    text_corpus = compact_text("|".join(
        str(value) for value in evidence.get("visible_text", [])
    ))
    contains = lambda corpus, markers: any(compact_text(marker) in corpus for marker in markers)
    card_carrier = contains(visual_corpus, (
        "白色广告卡片", "白色广告面板", "白色圆角卡片", "圆角白色卡片",
        "白色圆角矩形卡片", "圆角卡片容器", "展示牌", "屏幕",
    ))
    card_outside_relayout = contains(layout_corpus, (
        "上方图标卡片下方文字", "上方广告卡片下方文字", "上方卡片下方文字",
        "卡片外新增文案", "卡片外品牌文案", "上方完整广告下方营销文案",
    ))
    complete_insurance_copy = contains(text_corpus, (
        "投保须知", "保险条款", "具体费率", "保险金额", "责任范围", "承保",
    ))
    outer_background = contains(visual_corpus, (
        "渐变背景", "蓝白渐变背景", "蓝色渐变背景", "抽象装饰背景",
        "几何立方体线条", "立方体线条", "六边形网格背景", "网格背景",
    ))
    dense_marketing_content = (
        len(evidence.get("visible_text", [])) >= 6
        and len(evidence.get("action_keywords", [])) >= 2
    )
    return bool(
        card_carrier
        and card_outside_relayout
        and complete_insurance_copy
        and outer_background
        and dense_marketing_content
    )


def score_weighted_rules(
    corpus: str,
    positive: list[tuple[str, int]],
    negative: list[tuple[str, int]] | None = None,
) -> tuple[int, list[str]]:
    score = 0
    hits: list[str] = []
    for pattern, weight in positive:
        if compact_text(pattern) in corpus:
            score += weight
            hits.append(pattern)
    for pattern, penalty in negative or []:
        if compact_text(pattern) in corpus:
            score -= penalty
            hits.append(f"排除:{pattern}")
    return score, hits


def confidence_from_score(
    score: int,
    margin: int,
    evidence_confidence: float,
    uncertain_count: int,
) -> float:
    score_confidence = min(0.99, 0.72 + max(0, score) * 0.018 + max(0, margin) * 0.004)
    evidence_cap = 0.72 + evidence_confidence * 0.28
    penalty = min(0.12, uncertain_count * 0.025)
    return max(0.0, min(score_confidence, evidence_cap) - penalty)


def choose_candidate(
    scores: dict[str, tuple[int, list[str]]],
    evidence_confidence: float,
    uncertain_count: int,
    minimum_score: int,
) -> tuple[str | None, float, int, int, list[str]]:
    ordered = sorted(scores.items(), key=lambda item: item[1][0], reverse=True)
    if not ordered:
        return None, 0.0, 0, 0, []
    name, (top_score, hits) = ordered[0]
    second_score = ordered[1][1][0] if len(ordered) > 1 else 0
    margin = top_score - max(0, second_score)
    confidence = confidence_from_score(top_score, margin, evidence_confidence, uncertain_count)
    if top_score < minimum_score or (second_score > 0 and margin < 3):
        return None, confidence, top_score, margin, hits
    return name, confidence, top_score, margin, hits


def legacy_direction_bonus(direction: str, corpus: str) -> tuple[int, list[str]]:
    """旧规则的组合加分，确保已经 AUTO 的历史结果保持不变。"""
    bonus = 0
    hits: list[str] = []
    earning = any(compact_text(word) in corpus for word in ("红包", "金币", "提现", "现金收益", "赚钱"))
    female = any(compact_text(word) in corpus for word in ("女性人物", "美女", "女人"))
    family = any(compact_text(word) in corpus for word in ("家庭", "亲子", "父母和儿童", "儿童"))
    illustration = any(compact_text(word) in corpus for word in ("插画", "卡通", "绘制人物"))
    nested = any(compact_text(word) in corpus for word in ("嵌套广告截图", "原广告截图", "二次组合模板"))
    decorated = any(compact_text(word) in corpus for word in ("房间装饰背景", "装饰背景"))

    if direction == "美女网赚" and earning and female:
        bonus += 16
        hits.append("组合:女性人物+收益")
    if direction == "插画" and family and illustration:
        bonus += 12
        hits.append("合并:家庭亲子插画归入插画")
    if direction == "妙思自动衍生" and nested and decorated:
        bonus += 12
        hits.append("组合:嵌套素材+装饰背景")
    if direction == "自动衍生" and nested and any(
        compact_text(word) in corpus for word in ("手机号确认", "惊喜放送", "原素材面板")
    ):
        bonus += 12
        hits.append("组合:嵌套素材+确认文案")
    return bonus, hits


def medical_moon_grid_hint(corpus: str, image_name: str) -> bool:
    """识别医疗险“中心月亮/星球 + 九宫格环绕”的整体构图。

    这类素材经常被 API 写成“图标主导”，但九个环绕圆点是月亮方向的
    构图特征，而不是功能图标列表。文件名中的“月亮”只参与弱提示，必须
    与圆点/九宫格和夜空、宇宙、球体等上下文同时出现才触发。
    """
    grid_marker = any(
        compact_text(word) in corpus
        for word in (
            "九宫格", "九宫格排列", "九宫格环绕", "3x3排列", "八个圆点按钮",
            "八个圆点", "八个白色圆点", "八个白色圆点圆圈", "八个圆形节点",
            "环绕圆形节点按钮", "环绕圆形靶心按钮图标", "气泡状圆形图标",
            "小型透明圆点", "圆点图标", "圆点按钮", "同心圆盘",
        )
    )
    if not grid_marker:
        return False
    context = any(
        compact_text(word) in corpus
        for word in (
            "月亮", "月牙", "月相", "星空", "夜空", "夜景", "宇宙", "太空",
            "星球", "球体", "海洋背景", "湖面", "湖面场景", "水面倒影", "发光球体",
        )
    )
    filename_month = compact_text("月亮") in compact_text(image_name)
    return context or filename_month


def direction_bonus(direction: str, corpus: str, product: str | None = None) -> tuple[int, list[str]]:
    bonus = 0
    hits: list[str] = []
    earning = any(compact_text(word) in corpus for word in ("红包", "金币", "提现", "现金收益", "赚钱"))
    female = any(compact_text(word) in corpus for word in ("女性人物", "美女", "女人"))
    family = any(compact_text(word) in corpus for word in ("家庭", "亲子", "父母和儿童", "儿童"))
    illustration = any(compact_text(word) in corpus for word in ("插画", "卡通", "绘制人物"))
    nested = any(compact_text(word) in corpus for word in ("嵌套广告截图", "原广告截图", "二次组合模板"))
    decorated = any(compact_text(word) in corpus for word in ("房间装饰背景", "装饰背景"))
    icon_layout = any(
        compact_text(word) in corpus
        for word in ("图标主导", "中心图标主导", "环绕式图标布局", "图标环绕布局", "图标阵列")
    )
    shield = any(compact_text(word) in corpus for word in ("盾牌", "盾牌图标", "大型盾牌"))
    multiple_icons = any(
        compact_text(word) in corpus
        for word in (
            "多个图标", "灯泡图标", "列表图标", "下载图标", "柱状图",
            "上升箭头", "图标阵列", "环绕式图标布局",
        )
    )
    cosmic = any(
        compact_text(word) in corpus
        for word in ("宇宙", "星空", "星云", "星球", "行星", "地球", "太空")
    )
    travel = any(
        compact_text(word) in corpus
        for word in ("旅游", "出游", "旅行", "地标风景", "游乐园", "旅游景点", "旅游目的地")
    )
    split_layout = any(
        compact_text(word) in corpus
        for word in ("左右分栏", "左文右图", "左图右文", "右文左图", "上下分栏")
    )

    if direction == "美女网赚" and earning and female:
        bonus += 16
        hits.append("组合:女性人物+收益")
    if direction == "美女" and female:
        bonus += 8
        hits.append("组合:女性人物")
        if product == "百度极速版":
            bonus += 12
            hits.append("组合:百度极速版美女画面")
    if direction == "美女" and travel:
        bonus -= 8
        hits.append("排除:旅游主题优先")
    if direction == "网赚" and earning and product in {"快手", "快手极速版", "百度极速版", "喜番免费短剧"} and not female:
        bonus += 8
        hits.append("组合:平台+收益")
    if direction == "网赚" and product == "百度极速版" and female:
        bonus -= 20
        hits.append("排除:百度极速版女性主体优先美女")
    if direction == "萌宠" and earning:
        bonus -= 5
        hits.append("排除:收益画面优先网赚")
    if direction == "插画" and family and illustration:
        bonus += 12
        hits.append("合并:家庭亲子插画归入插画")
    if direction == "妙思自动衍生" and nested and decorated:
        bonus += 12
        hits.append("组合:嵌套素材+装饰背景")
    if direction == GLOBAL_DERIVED_DIRECTION and product and derived_nested_hint(corpus):
        bonus += 20
        hits.append("组合:产品素材+完整原素材嵌入")
    if direction == "自动衍生" and nested and any(
        compact_text(word) in corpus for word in ("手机号确认", "惊喜放送", "原素材面板")
    ):
        bonus += 12
        hits.append("组合:嵌套素材+确认文案")
    if direction == "图标" and icon_layout:
        bonus += 8
        hits.append("组合:图标版式")
    if direction == "图标" and product and derived_nested_hint(corpus):
        bonus -= 24
        hits.append("排除:完整原素材嵌入优先自动衍生")
    if direction == "图标" and product == "百万医疗险" and shield and not multiple_icons:
        bonus += 8
        hits.append("合并:医疗险盾牌归入图标")
    if direction == "月亮" and product == "百万医疗险" and cosmic:
        bonus += 12
        hits.append("组合:医疗险+宇宙星球画面")
    if direction == "月亮" and product == "百万医疗险" and any(
        compact_text(word) in corpus for word in ("月亮", "月牙", "月相")
    ):
        bonus += 8
        hits.append("组合:医疗险+月相证据")
    if direction == "旅游" and travel:
        bonus += 8
        hits.append("组合:旅游场景")
    if direction == "图标" and product == "QQ阅读" and icon_layout:
        bonus += 8
        hits.append("组合:QQ阅读+图标版式")
    if direction == "插画" and product == "QQ阅读" and any(
        compact_text(word) in corpus
        for word in ("美食场景", "餐厅餐桌", "游乐园场景", "儿童活动场景", "卡通")
    ):
        bonus += 12
        hits.append("组合:QQ阅读插画场景")
    if direction == "上下左右图文" and product == "QQ阅读" and split_layout:
        bonus += 25
        hits.append("组合:QQ阅读分栏版式")
    return bonus, hits


def product_conflict(corpus: str) -> str | None:
    conflict_groups = [
        (
            "医疗险与防癌险证据同时出现",
            ("百万医疗险", "医疗险"),
            ("防癌险", "癌症保障", "恶性肿瘤"),
        ),
        (
            "百度普通版与极速版证据同时出现",
            ("百度app", "百度一下"),
            ("百度极速版", "极速金", "红色极速角标"),
        ),
        (
            "百度普通版与百度网盘证据同时出现",
            ("百度app", "百度一下"),
            ("百度网盘", "百度云盘", "百度网盘logo", "网盘超级会员"),
        ),
        (
            "百度网盘与百度极速版证据同时出现",
            ("百度网盘", "百度云盘", "百度网盘logo", "网盘超级会员"),
            ("百度极速版", "极速金", "红色极速角标"),
        ),
        (
            "快手普通版与极速版证据同时出现",
            ("快手app",),
            ("快手极速版", "极速版角标", "闪电角标"),
        ),
    ]
    for label, left_patterns, right_patterns in conflict_groups:
        left = any(compact_text(pattern) in corpus for pattern in left_patterns)
        right = any(compact_text(pattern) in corpus for pattern in right_patterns)
        if left and right:
            return label
    return None


def validate_overlapping_product(product: str, corpus: str, image_name: str = "") -> tuple[bool, str]:
    """对名称重叠的产品做第二层互斥验证。

    即使出现完整产品名，也必须再有 Logo、版本角标或专属场景交叉验证；
    没有完整名时，至少要求一个品牌家族证据和一个版本/产品专属证据。
    只有通用“百度”“快手”或家族 Logo 时拒绝强猜。
    """
    contains = lambda patterns: any(compact_text(pattern) in corpus for pattern in patterns)
    filename_product = filename_product_hint(image_name) if image_name else None
    filename_confirms = filename_product == product
    filename_directions = filename_direction_hints(image_name) if image_name else []

    baidu_logo = ("百度logo", "du熊掌logo", "蓝色du熊掌")
    baidu_family = ("百度",) + baidu_logo
    baidu_speed = (
        "极速金", "红色极速角标", "极速版底部角标", "极速角标",
        "看视频赚现金红包", "随时可提现",
    )
    baidu_netdisk = (
        "百度网盘logo", "蓝红三角网盘logo", "网盘超级会员", "网盘会员",
        "云存储", "网盘空间", "文件列表", "上传文件", "下载文件", "svip",
    )
    baidu_standard = ("搜索框", "搜索结果页", "资讯信息流", "百度一下按钮")

    kuaishou_logo = ("快手logo", "相机logo", "摄像机logo")
    kuaishou_family = ("快手",) + kuaishou_logo
    kuaishou_speed = ("极速版角标", "闪电角标", "快手极速角标")
    kuaishou_standard = (
        "橙色相机logo", "橙红色相机logo", "橙色双环logo",
        "橙色圆角方形双环白纹logo", "橙色方形白色摄像机图案logo",
        "橙色方形白色摄像机logo", "红色圆角方形白摄像机logo",
    )

    if product == "百度":
        if contains(("百度极速版", "百度网盘", "百度云盘")) or contains(baidu_speed + baidu_netdisk):
            return False, "百度普通版存在极速版或网盘专属证据"
        if contains(("百度app", "百度一下")) and contains(baidu_logo + baidu_standard):
            return True, "普通版名称/文案+Logo或搜索场景复核"
        if contains(baidu_family) and contains(baidu_standard):
            return True, "百度家族证据+普通搜索场景证据"
        if filename_confirms and contains(baidu_logo):
            return True, "文件名百度产品词+画面百度Logo复核"
        if filename_confirms and filename_directions and not contains(baidu_speed + baidu_netdisk):
            return True, "文件名百度产品词+文件名方向+无极速/网盘冲突"
        if filename_confirms and corpus and not contains(baidu_speed + baidu_netdisk):
            return True, "文件名百度产品词+其他视觉证据+无极速/网盘冲突"
        return False, "只有百度通用品牌证据，缺少百度App/百度一下或搜索场景复核"

    if product == "百度网盘":
        if contains(("百度网盘", "百度云盘")) and contains(baidu_netdisk):
            return True, "完整百度网盘名称+Logo或网盘功能复核"
        if contains(baidu_family) and contains(baidu_netdisk):
            return True, "百度家族证据+网盘专属证据"
        if filename_confirms and contains(baidu_netdisk):
            return True, "文件名百度网盘产品词+画面网盘专属证据复核"
        if filename_confirms and filename_directions and not contains(baidu_speed):
            return True, "文件名百度网盘产品词+文件名方向+无极速冲突"
        if filename_confirms and corpus and not contains(baidu_speed):
            return True, "文件名百度网盘产品词+其他视觉证据+无极速冲突"
        return False, "缺少完整百度网盘名称，或缺少百度品牌与网盘功能的交叉证据"

    if product == "百度极速版":
        if contains(("百度极速版",)) and contains(baidu_logo + baidu_speed):
            return True, "完整百度极速版名称+Logo或极速版特征复核"
        if contains(baidu_family) and contains(baidu_speed):
            return True, "百度家族证据+极速版专属角标/收益证据"
        if filename_confirms and contains(baidu_logo + baidu_speed):
            return True, "文件名百度极速版产品词+画面Logo/角标复核"
        if filename_confirms and filename_directions and not contains(baidu_netdisk):
            return True, "文件名百度极速版产品词+文件名方向+无网盘冲突"
        if filename_confirms and corpus and not contains(baidu_netdisk):
            return True, "文件名百度极速版产品词+其他视觉证据+无网盘冲突"
        return False, "缺少完整百度极速版名称，或缺少百度品牌与极速版角标的交叉证据"

    if product == "快手":
        if contains(("快手极速版",)) or contains(kuaishou_speed):
            return False, "快手普通版存在极速版专属证据"
        if contains(("快手app",)) and contains(kuaishou_logo + kuaishou_standard):
            return True, "完整快手App名称+普通版Logo复核"
        if contains(kuaishou_family) and contains(kuaishou_standard):
            return True, "快手家族证据+普通版Logo形态"
        if filename_confirms and contains(kuaishou_logo + kuaishou_standard):
            return True, "文件名快手产品词+画面普通版Logo复核"
        if filename_confirms and filename_directions and not contains(kuaishou_speed):
            return True, "文件名快手产品词+文件名方向+无极速版冲突"
        if filename_confirms and corpus and not contains(kuaishou_speed):
            return True, "文件名快手产品词+其他视觉证据+无极速版冲突"
        return False, "只有快手通用品牌证据，缺少快手App名称或普通版Logo复核"

    if product == "快手极速版":
        if contains(("快手极速版",)) and contains(kuaishou_logo + kuaishou_speed):
            return True, "完整快手极速版名称+Logo或极速版角标复核"
        if contains(kuaishou_family) and contains(kuaishou_speed):
            return True, "快手家族证据+极速版闪电/角标证据"
        if filename_confirms and contains(kuaishou_logo + kuaishou_speed):
            return True, "文件名快手极速版产品词+画面Logo/角标复核"
        if filename_confirms and filename_directions and not contains(kuaishou_standard):
            return True, "文件名快手极速版产品词+文件名方向+无普通版冲突"
        if filename_confirms and corpus and not contains(kuaishou_standard):
            return True, "文件名快手极速版产品词+其他视觉证据+无普通版冲突"
        return False, "缺少完整快手极速版名称，或缺少快手品牌与极速版角标的交叉证据"

    return True, "非名称重叠产品"


def external_direction_name(direction: str) -> str:
    return {
        "左右图文": "上下左右图文",
        "萌宠": "宠物",
        "文字大字报": "大字报",
    }.get(direction, direction)


PET_VISUAL_MARKERS: tuple[str, ...] = (
    "宠物", "萌宠", "猫咪", "小猫", "猫爪", "猫", "狗狗", "小狗", "狗爪", "狗",
    "柴犬", "比熊犬", "犬", "卷毛小狗", "爪印", "白狮幼崽", "狮子幼崽", "动物主体",
)


def has_pet_visual_evidence(evidence: dict[str, Any]) -> bool:
    """宠物方向必须由画面/场景中的动物语义触发，文案、价格和版式不能代替。"""
    visual_parts = [
        *evidence.get("visual_keywords", []),
        *evidence.get("scene_keywords", []),
    ]
    visual_corpus = compact_text("|".join(str(value) for value in visual_parts))
    return any(compact_text(marker) in visual_corpus for marker in PET_VISUAL_MARKERS)


def has_layout_marker(evidence: dict[str, Any], marker: str) -> bool:
    layout_corpus = compact_text("|".join(
        str(value) for value in evidence.get("layout_keywords", [])
    ))
    return compact_text(marker) in layout_corpus


def is_admuse_square_image(raw: dict[str, Any], platform: str) -> bool:
    """妙思方图统一归朋友圈；允许 3% 的编码或裁切尺寸误差。"""
    if platform != "admuse":
        return False
    try:
        width = int(raw.get("_image_width", 0))
        height = int(raw.get("_image_height", 0))
    except (TypeError, ValueError):
        return False
    if width <= 0 or height <= 0:
        return False
    return abs((width / height) - 1.0) <= 0.03


def has_pet_dominant_evidence(evidence: dict[str, Any]) -> bool:
    return (
        has_layout_marker(evidence, "宠物第一视觉主体")
        and has_pet_visual_evidence(evidence)
    )


STRONG_MOON_VISUAL_MARKERS: tuple[str, ...] = (
    "发光满月", "发光月亮", "发光月球", "月相特写", "月相", "弯月", "新月", "月牙",
)
MOONLIKE_CELESTIAL_MARKERS: tuple[str, ...] = (
    "发光星球", "发光水晶球", "发光圆球", "星球体", "星球", "行星", "天体",
)
GENERIC_CELESTIAL_SPHERE_MARKERS: tuple[str, ...] = (
    "中心球体", "中央球体", "中央黑色球体", "黑色球体", "大型球体", "发光球体",
)
ORBITAL_COMPOSITION_MARKERS: tuple[str, ...] = (
    "圆环", "轨道", "同心圆", "星环", "行星环", "环绕", "圆盘阵列", "多个圆盘", "多圆盘",
)
MOON_NIGHT_SCENE_MARKERS: tuple[str, ...] = (
    "星空", "夜空", "月夜", "夜晚海洋", "海面倒影", "月光",
)


def has_strong_moon_visual_evidence(evidence: dict[str, Any]) -> bool:
    """只用画面与场景确认月亮，避免保险权益文案把月亮推成图标。"""
    if (
        has_layout_marker(evidence, "插画场景第一视觉主体")
        and not has_layout_marker(evidence, "月亮球体第一视觉主体")
    ):
        return False
    if has_layout_marker(evidence, "月亮球体第一视觉主体"):
        return True
    if evidence.get("graphic_type") == "月亮/天体":
        return True
    relation = str(evidence.get("composition_relation", "不明确"))
    structure = str(evidence.get("composition_structure", "不明确"))
    non_moon_primary = (
        relation in {"文字主导", "图文均衡"}
        or structure in {"左右图文", "上下图文", "多区块图文"}
        or any(
            has_layout_marker(evidence, marker)
            for marker in ("文字第一视觉主体", "均衡图文版式", "宠物第一视觉主体")
        )
    )
    if non_moon_primary:
        return False
    visual_parts = [
        *evidence.get("visual_keywords", []),
        *evidence.get("scene_keywords", []),
        *evidence.get("layout_keywords", []),
    ]
    visual_corpus = compact_text("|".join(str(value) for value in visual_parts))
    if any(compact_text(marker) in visual_corpus for marker in STRONG_MOON_VISUAL_MARKERS):
        return True
    celestial = any(
        compact_text(marker) in visual_corpus
        for marker in MOONLIKE_CELESTIAL_MARKERS
    )
    night_scene = any(
        compact_text(marker) in visual_corpus
        for marker in MOON_NIGHT_SCENE_MARKERS
    )
    generic_sphere = any(
        compact_text(marker) in visual_corpus
        for marker in GENERIC_CELESTIAL_SPHERE_MARKERS
    )
    orbital_composition = any(
        compact_text(marker) in visual_corpus
        for marker in ORBITAL_COMPOSITION_MARKERS
    )
    return (celestial and night_scene) or (generic_sphere and orbital_composition)


def has_strong_illustration_visual_evidence(evidence: dict[str, Any]) -> bool:
    """人物/家庭/情侣是画面主体时，夜景、月亮和星空只能作为插画背景。

    只有在没有明确图标证据、也没有大面积留白时，才允许这条强规则直接
    抢占方向；否则保留证据让图标/图文候选继续竞争，避免把稀疏图标误分成插画。
    """
    if has_explicit_icon_keyword_evidence(evidence) or has_large_negative_space_evidence(evidence):
        return False
    graphic_type = str(evidence.get("graphic_type", "不明确"))
    visual_corpus = compact_text("|".join(
        str(value) for value in evidence.get("visual_keywords", [])
    ))
    scene_corpus = compact_text("|".join(
        str(value) for value in evidence.get("scene_keywords", [])
    ))
    layout_corpus = compact_text("|".join(
        str(value) for value in evidence.get("layout_keywords", [])
    ))
    illustration = any(
        compact_text(marker) in visual_corpus
        for marker in (
            "卡通情侣插画", "家庭亲子插画", "卡通人物插画", "人物插画",
            "卡通家庭插画", "家庭插画", "卡通插画", "手绘插画", "水彩插画",
        )
    )
    photographic_people = any(
        compact_text(marker) in visual_corpus
        for marker in ("人物照片", "真人", "摄影", "实拍")
    )
    if graphic_type == "人物照片" and photographic_people:
        return False
    illustration_medium = graphic_type == "插画" or illustration
    if has_layout_marker(evidence, "插画场景第一视觉主体"):
        return illustration_medium
    people = any(
        compact_text(marker) in f"{visual_corpus}|{scene_corpus}"
        for marker in (
            "情侣", "家庭", "人物", "老人", "儿童", "男孩", "女孩",
            "父亲", "母亲", "祖父", "祖母", "三代人物",
        )
    )
    structured_layout = any(
        compact_text(marker) in layout_corpus
        for marker in (
            "左右分栏", "左文右图", "左图右文", "多区块图文", "图标列表",
            "信息卡片列表", "列表式卖点", "四列按钮", "三栏卡片", "图文混排",
            "左侧插画右侧文字", "左侧插画+右侧文字区", "左侧文字右侧插画",
        )
    )
    return illustration_medium and people and not structured_layout


LEGAL_TEXT_MARKERS: tuple[str, ...] = (
    "保险条款", "投保须知", "具体费率", "实际投保", "图片纯属", "未经授权",
    "免责声明", "详情请见", "本产品由", "代理销售", "承保公司",
)
SCENERY_MARKERS: tuple[str, ...] = (
    "纯自然风景", "自然风景", "风景照片", "全幅风景", "森林", "树林", "树木",
    "山川", "山景", "草原", "草地", "湖泊", "湖面", "海面", "海洋", "蓝天",
    "云层", "白云", "日出", "日落", "雪景", "田野", "峡谷", "瀑布",
    "城市风景", "城市风光", "城市天际线", "城市建筑全景", "城市全景",
    "建筑群全景", "海滨风景", "海滨城市", "海湾城市",
)
COMPETING_VISUAL_MARKERS: tuple[str, ...] = (
    "图标", "盾牌", "锁形", "挂锁", "云朵图标", "漏斗", "剪贴板", "文档",
    "胶囊", "药片", "数据图表", "柱状图", "饼图", "权益卡片", "信息卡片",
    "人物", "家庭", "儿童", "男孩", "女孩", "宠物", "猫", "狗", "动物",
    "汽车", "产品", "插画", "3d主体", "3d图案", "立体主体",
)
ICON_LIKE_GRAPHIC_MARKERS: tuple[str, ...] = (
    "图标", "盾牌", "锁形", "挂锁", "云朵", "漏斗", "剪贴板", "文档",
    "胶囊", "药片", "数据图表", "柱状图", "饼图", "权益卡片", "信息卡片",
    "3d主体", "3d图案", "立体主体", "圆形平台", "数据节点",
)

# 这些证据表示模型明确看到了一个图标/符号主体，而不是仅仅看到了
# “信息卡片”或“图文排版”。它们用于处理插画与图标同时出现时的冲突。
EXPLICIT_ICON_EVIDENCE_MARKERS: tuple[str, ...] = (
    "中心图标第一视觉主体", "图标主导", "中心图标主导", "大型图标主导",
    "单一大型图标", "主体图标", "3d图标", "立体图标", "功能图标",
    "权益图标", "盾牌图标", "锁形图标", "挂锁图标", "云朵图标",
    "漏斗图标", "剪贴板图标", "文档图标", "胶囊图标", "药片图标",
)
DECORATIVE_ICON_MARKERS: tuple[str, ...] = (
    "复选框图标", "对勾图标", "勾选图标", "按钮图标", "列表图标",
    "小图标", "装饰图标", "角标图标", "信息图标", "图标加文字列表",
    "图标+文字列表",
)
LARGE_NEGATIVE_SPACE_MARKERS: tuple[str, ...] = (
    "大面积留白", "大面积浅色留白", "大面积空白", "大面积纯色留白",
    "大片留白", "留白区域较大", "背景留白较多", "宽阔留白", "四周留白",
    "留白充足", "低信息量留白",
)


def meaningful_marketing_text_count(evidence: dict[str, Any]) -> int:
    values = [
        compact_text(str(value))
        for value in evidence.get("visible_text", [])
        if str(value).strip()
    ]
    return sum(
        1
        for value in values
        if not any(compact_text(marker) in value for marker in LEGAL_TEXT_MARKERS)
    )


def has_competing_visual_subject(evidence: dict[str, Any]) -> bool:
    explicit = evidence.get("competing_visual_subject")
    if explicit is True:
        return True
    if evidence.get("graphic_type") in {"图标", "插画", "人物照片", "产品照片"}:
        return True
    visual_corpus = compact_text("|".join(
        str(value) for value in evidence.get("visual_keywords", [])
    ))
    return any(compact_text(marker) in visual_corpus for marker in COMPETING_VISUAL_MARKERS)


def has_icon_like_graphic(evidence: dict[str, Any]) -> bool:
    if evidence.get("graphic_type") == "图标":
        return True
    visual_layout_corpus = compact_text("|".join([
        *(str(value) for value in evidence.get("visual_keywords", [])),
        *(str(value) for value in evidence.get("layout_keywords", [])),
    ]))
    return any(
        compact_text(marker) in visual_layout_corpus
        for marker in ICON_LIKE_GRAPHIC_MARKERS
    )


def has_explicit_icon_keyword_evidence(evidence: dict[str, Any]) -> bool:
    """只认 API 明确提到的图标/符号，不把普通信息卡片当成图标。"""
    if evidence.get("graphic_type") == "图标":
        return True
    values = [
        *(str(value) for value in evidence.get("visual_keywords", [])),
        *(str(value) for value in evidence.get("layout_keywords", [])),
    ]
    for value in values:
        normalized = compact_text(value)
        if any(compact_text(marker) in normalized for marker in DECORATIVE_ICON_MARKERS):
            continue
        if normalized in {"图标", "图标特征", "图标主体"}:
            return True
        if any(compact_text(marker) in normalized for marker in EXPLICIT_ICON_EVIDENCE_MARKERS):
            return True
    return False


def has_large_negative_space_evidence(evidence: dict[str, Any]) -> bool:
    """读取 API 对大面积留白的客观描述；不能用背景颜色推断留白。"""
    corpus = compact_text("|".join([
        *(str(value) for value in evidence.get("visual_keywords", [])),
        *(str(value) for value in evidence.get("layout_keywords", [])),
        *(str(value) for value in evidence.get("scene_keywords", [])),
    ]))
    return any(compact_text(marker) in corpus for marker in LARGE_NEGATIVE_SPACE_MARKERS)


def has_sparse_icon_illustration_conflict(evidence: dict[str, Any]) -> bool:
    """插画与图标并存且大面积留白时，按用户校准归入图标方向。"""
    graphic_type = str(evidence.get("graphic_type", "不明确"))
    visual_corpus = compact_text("|".join(
        str(value) for value in evidence.get("visual_keywords", [])
    ))
    illustration = graphic_type == "插画" or any(
        compact_text(marker) in visual_corpus
        for marker in ("插画", "卡通", "手绘", "水彩", "绘制人物")
    )
    return (
        illustration
        and has_explicit_icon_keyword_evidence(evidence)
        and has_large_negative_space_evidence(evidence)
    )


def has_center_single_icon_evidence(evidence: dict[str, Any]) -> bool:
    """中心单图标优先于均衡/上下图文字段，底部权益条不改变主体。"""
    graphic_type = str(evidence.get("graphic_type", "不明确"))
    if graphic_type == "月亮/天体" or has_strong_moon_visual_evidence(evidence):
        return False
    if any(
        has_layout_marker(evidence, marker)
        for marker in ("插画场景第一视觉主体", "宠物第一视觉主体")
    ):
        return False
    if graphic_type not in {"图标", "不明确"}:
        return False
    if not has_icon_like_graphic(evidence):
        return False
    layout_corpus = compact_text("|".join(
        str(value) for value in evidence.get("layout_keywords", [])
    ))
    structure = str(evidence.get("composition_structure", "不明确"))
    center_icon_marker = any(
        compact_text(marker) in layout_corpus
        for marker in ("中心图标第一视觉主体", "中心图标主导", "单一大型图标")
    )
    if structure == "中心单主体":
        return True
    left_right_or_text_primary = any(
        compact_text(marker) in layout_corpus
        for marker in (
            "左右分栏", "左文右图", "左图右文", "右文左图", "图文并列版式",
            "左侧文字区右侧图标区", "文字第一视觉主体",
        )
    )
    return center_icon_marker and not left_right_or_text_primary


SIMPLE_ABSTRACT_BACKGROUND_MARKERS: tuple[str, ...] = (
    "纯色背景", "单色背景", "简单渐变", "渐变背景", "抽象装饰背景", "几何色块背景",
    "圆形装饰色块", "低信息量抽象", "纸张承载框", "票券卡片",
)
DECORATIVE_TEXT_FRAME_MARKERS: tuple[str, ...] = (
    "对话框", "对话气泡", "文字气泡", "气泡边框", "插画边框", "描边边框",
    "叶子装饰", "爱心装饰", "水滴装饰", "加号装饰", "星星装饰", "四角装饰",
    "抽象色块", "装饰色块", "装饰元素",
)
PRIMARY_VISUAL_SUBJECT_MARKERS: tuple[str, ...] = (
    "人物", "家庭", "儿童", "男孩", "女孩", "汽车", "电动车", "手机", "设备",
    "产品主体", "盾牌", "锁形", "挂锁", "宠物", "猫", "狗", "动物", "月亮",
    "星球", "球体", "圆环主体", "数据图表", "信息卡片列表",
)


def has_simple_abstract_background_evidence(evidence: dict[str, Any]) -> bool:
    background_type = str(evidence.get("background_type", "不明确"))
    if background_type in {"纯色/简单抽象", "抽象渐变"}:
        return not has_competing_visual_subject(evidence)
    visual_scene_corpus = compact_text("|".join([
        *(str(value) for value in evidence.get("visual_keywords", [])),
        *(str(value) for value in evidence.get("scene_keywords", [])),
    ]))
    return (
        any(compact_text(marker) in visual_scene_corpus for marker in SIMPLE_ABSTRACT_BACKGROUND_MARKERS)
        and not has_competing_visual_subject(evidence)
    )


def has_scenery_background_evidence(evidence: dict[str, Any]) -> bool:
    background_type = str(evidence.get("background_type", "不明确"))
    visual_scene_corpus = compact_text("|".join([
        *(str(value) for value in evidence.get("visual_keywords", [])),
        *(str(value) for value in evidence.get("scene_keywords", [])),
        *(str(value) for value in evidence.get("layout_keywords", [])),
    ]))
    scenery = background_type in {"纯自然风景", "城市风景"} or any(
        compact_text(marker) in visual_scene_corpus for marker in SCENERY_MARKERS
    )
    return scenery and not has_competing_visual_subject(evidence)


def has_decorative_text_frame_evidence(evidence: dict[str, Any]) -> bool:
    """文字气泡和边角装饰是承载背景，不是独立插画或图标主体。"""
    structure = str(evidence.get("composition_structure", "不明确"))
    if structure not in {"中心单主体", "不明确", ""}:
        return False
    visual_layout_corpus = compact_text("|".join([
        *(str(value) for value in evidence.get("visual_keywords", [])),
        *(str(value) for value in evidence.get("layout_keywords", [])),
    ]))
    if not any(
        compact_text(marker) in visual_layout_corpus
        for marker in DECORATIVE_TEXT_FRAME_MARKERS
    ):
        return False
    visual_corpus = compact_text("|".join(
        str(value) for value in evidence.get("visual_keywords", [])
    ))
    return not any(
        compact_text(marker) in visual_corpus
        for marker in PRIMARY_VISUAL_SUBJECT_MARKERS
    )


def has_simple_poster_background_evidence(evidence: dict[str, Any]) -> bool:
    return (
        has_scenery_background_evidence(evidence)
        or has_simple_abstract_background_evidence(evidence)
        or has_decorative_text_frame_evidence(evidence)
    )


def has_balanced_text_graphic_evidence(evidence: dict[str, Any]) -> bool:
    """图案是什么与整图版式分开；分栏且双方承担信息时优先图文方向。"""
    if has_center_single_icon_evidence(evidence):
        return False
    relation = str(evidence.get("composition_relation", "不明确"))
    structure = str(evidence.get("composition_structure", "不明确"))
    if relation == "图文均衡":
        return True

    layout_corpus = compact_text("|".join(
        str(value) for value in evidence.get("layout_keywords", [])
    ))
    explicit_balance = compact_text("均衡图文版式") in layout_corpus
    if explicit_balance:
        return True

    split_layout = structure in {"左右图文", "上下图文"} or any(
        compact_text(marker) in layout_corpus
        for marker in (
            "左右分栏", "左文右图", "左图右文", "右文左图", "左侧文字区右侧图标区",
            "左侧文字区右侧视觉主体区", "图文并列版式", "上下分栏", "上文下图", "下文上图",
        )
    )
    multi_block_layout = structure == "多区块图文" or any(
        compact_text(marker) in layout_corpus
        for marker in ("多区块图文", "信息卡片列表", "多行权益列表", "列表式卖点", "按钮区", "上下结构")
    )
    text_led = any(
        compact_text(marker) in layout_corpus
        for marker in ("文字第一视觉主体", "大号文字主导", "大号标题主导", "竖版大字标题")
    )
    strong_center_icon = any(
        compact_text(marker) in layout_corpus
        for marker in ("中心图标第一视觉主体", "中心图标主导", "大型图标主导", "单一大型图标")
    )
    primary_conflict = (
        compact_text("文字第一视觉主体") in layout_corpus
        and compact_text("中心图标第一视觉主体") in layout_corpus
    )
    meaningful_text = meaningful_marketing_text_count(evidence)
    has_visual_subject = (
        has_icon_like_graphic(evidence)
        or has_competing_visual_subject(evidence)
    )
    if not has_visual_subject:
        return False
    if split_layout and meaningful_text >= 2:
        return True
    if primary_conflict and meaningful_text >= 2:
        return True
    if multi_block_layout and text_led and meaningful_text >= 3 and not (
        strong_center_icon and not primary_conflict
    ):
        return True
    return False


def has_strong_icon_visual_evidence(evidence: dict[str, Any]) -> bool:
    """识别真正的单一图标主体；均衡图文不能因图案类型是图标而进入图标方向。"""
    if has_strong_moon_visual_evidence(evidence):
        return False
    if has_derived_nested_evidence(evidence):
        return False
    if has_balanced_text_graphic_evidence(evidence):
        return False
    if not has_icon_like_graphic(evidence):
        return False
    center_single_icon = has_center_single_icon_evidence(evidence)
    relation = str(evidence.get("composition_relation", "不明确"))
    if relation in {"文字主导", "图文均衡"} and not center_single_icon:
        return False
    explicit_dominance = (
        center_single_icon
        or
        relation == "图案主导"
        or has_layout_marker(evidence, "中心图标第一视觉主体")
    )
    visual_corpus = compact_text("|".join(
        str(value) for value in evidence.get("visual_keywords", [])
    ))
    layout_corpus = compact_text("|".join(
        str(value) for value in evidence.get("layout_keywords", [])
    ))
    explicit_icon = any(
        compact_text(marker) in f"{visual_corpus}|{layout_corpus}"
        for marker in (
            "图标主导", "中心图标主导", "大型图标主导", "大型盾牌",
            "盾牌主导", "大型锁形图标", "黄色挂锁", "锁形图标主导",
        )
    )
    structured_layout = any(
        compact_text(marker) in layout_corpus
        for marker in (
            "左右分栏", "左文右图", "左图右文", "多区块图文", "图标列表",
            "列表式卖点", "三栏卡片", "卡片式布局", "信息卡片列表",
        )
    )
    center_surrounded_icons = bool(
        any(
            compact_text(marker) in layout_corpus
            for marker in (
                "四角图标分布", "四角图标环绕", "四角小图标",
                "环绕式图标布局", "图标环绕布局",
            )
        )
        and any(
            compact_text(marker) in layout_corpus
            for marker in (
                "中心主体构图", "中心3d主体构图", "中心主体突出",
                "中心主体", "中心图标主导", "居中主体图标",
            )
        )
        and compact_text("图标") in visual_corpus
    )
    return (
        (explicit_dominance or (explicit_icon and not structured_layout) or center_surrounded_icons)
        and not has_strong_illustration_visual_evidence(evidence)
        and not has_pet_visual_evidence(evidence)
    )


def has_text_led_poster_evidence(evidence: dict[str, Any]) -> bool:
    """大字报以文字为主体，背景只能是风景或低信息量纯色/抽象装饰。"""
    relation = str(evidence.get("composition_relation", "不明确"))
    structure = str(evidence.get("composition_structure", "不明确"))
    layout_corpus = compact_text("|".join(
        str(value) for value in evidence.get("layout_keywords", [])
    ))
    if structure in {"左右图文", "上下图文", "多区块图文"}:
        return False
    if any(
        compact_text(marker) in layout_corpus
        for marker in (
            "左右分栏", "上下分栏", "多区块图文", "图文并列版式", "信息卡片列表",
            "权益卡片", "按钮区", "图标列表",
        )
    ):
        return False
    overlay = evidence.get("text_overlay_on_scenery")
    explicit_overlay = structure == "文字覆盖风景" or any(
        compact_text(marker) in layout_corpus
        for marker in ("全幅风景底图", "中心文字浮层", "文字覆盖风景", "风景底图")
    )
    scenery_overlay = (
        has_scenery_background_evidence(evidence)
        and meaningful_marketing_text_count(evidence) >= 1
        and (overlay is True or explicit_overlay)
    )
    if scenery_overlay:
        return True
    if relation in {"图案主导", "图文均衡"}:
        return False
    text_led = relation == "文字主导" or any(
        compact_text(marker) in layout_corpus
        for marker in ("文字第一视觉主体", "大号文字主导", "大标题主导", "竖版大字标题")
    )
    if not text_led or not has_simple_poster_background_evidence(evidence):
        return False
    if has_decorative_text_frame_evidence(evidence):
        return meaningful_marketing_text_count(evidence) >= 2
    if has_simple_abstract_background_evidence(evidence):
        return True
    return overlay is True or explicit_overlay or overlay is None


def shooting_monitor_derived_hint(evidence: dict[str, Any]) -> bool:
    """识别摄像机/拍摄场景中监视器承载完整保险广告的二次衍生图。"""
    visual_corpus = compact_text("|".join(
        str(value) for value in evidence.get("visual_keywords", [])
    ))
    scene_corpus = compact_text("|".join(
        str(value) for value in evidence.get("scene_keywords", [])
    ))
    text_corpus = compact_text("|".join([
        *(str(value) for value in evidence.get("visible_text", [])),
        *(str(value) for value in evidence.get("action_keywords", [])),
    ]))
    camera_and_monitor = (
        any(compact_text(marker) in visual_corpus for marker in ("摄像机", "摄影机"))
        and any(compact_text(marker) in visual_corpus for marker in ("监视器屏幕", "监视器", "拍摄屏幕"))
    )
    tripod_monitor = (
        any(compact_text(marker) in visual_corpus for marker in ("监视器屏幕", "监视器"))
        and any(compact_text(marker) in visual_corpus for marker in ("三脚架设备", "三脚架"))
    )
    shooting_scene = any(
        compact_text(marker) in scene_corpus
        for marker in ("影视拍摄场景", "摄影棚", "拍摄场景", "设备操作场景")
    )
    inner_ad_content = (
        any(compact_text(marker) in visual_corpus for marker in ("锁图标", "锁形图案", "盾牌图标"))
        and any(compact_text(marker) in text_corpus for marker in ("领取保险", "保险", "投保", "保障"))
    )
    return (camera_and_monitor and shooting_scene and inner_ad_content) or (
        tripod_monitor and inner_ad_content
    )


def external_direction_bonus(
    direction: str,
    corpus: str,
) -> tuple[int, list[str]]:
    bonus = 0
    hits: list[str] = []
    contains = lambda patterns: any(compact_text(pattern) in corpus for pattern in patterns)
    animals = contains((
        "宠物", "宠物主导", "宠物特写", "宠物摄影", "猫咪", "小猫", "猫爪",
        "狗狗", "小狗", "狗爪", "柴犬", "卷毛小狗", "爪印图案",
    ))
    split_layout = contains((
        "左右分栏", "左文右图", "左图右文", "右文左图", "上下分栏",
        "多区块图文", "组合信息区块", "图标列表式排版",
    ))
    left_right_layout = contains(("左右分栏", "左文右图", "左图右文", "右文左图"))
    illustration_layout = contains((
        "左侧插画右侧文字", "左侧文字右侧插画", "背景插画主导",
        "插画在上文字在下", "上插画下文字", "上部插画下部文字", "上图下文",
    ))
    information_blocks = contains((
        "多区块图文", "组合信息区块", "图标列表", "图标+文字列表",
        "列表式权益展示", "列表式卖点", "左右信息区",
    ))
    illustration = contains(("卡通插画", "家庭亲子插画", "人物插画", "插画主导"))

    if direction == "宠物" and animals:
        bonus += 18
        hits.append("组合:猫狗宠物主体")
    if direction == "插画" and animals and not contains(("人物", "家庭", "亲子", "儿童")):
        bonus -= 14
        hits.append("排除:猫狗宠物主体优先宠物")
    if direction == "插画" and illustration:
        bonus += 8
        hits.append("组合:插画主体")
    if direction == "插画" and contains(("插画场景第一视觉主体",)):
        bonus += 80
        hits.append("组合:API确认插画场景为第一视觉主体")
    if direction == "大字报" and contains(("文字第一视觉主体",)):
        bonus += 80
        hits.append("组合:API确认文字为第一视觉主体")
    if direction == "月亮" and contains(("月亮球体第一视觉主体",)):
        bonus += 80
        hits.append("组合:API确认月亮球体为第一视觉主体")
    if direction == "图标" and contains(("中心图标第一视觉主体",)):
        bonus += 80
        hits.append("组合:API确认中心图标为第一视觉主体")
    if direction == "上下左右图文" and contains(("均衡图文版式",)):
        bonus += 80
        hits.append("组合:API确认图文区域视觉重量均衡")
    if direction == "上下左右图文" and left_right_layout and not illustration_layout:
        bonus += 70
        hits.append("组合:明确左右信息分栏")
    elif direction == "上下左右图文" and left_right_layout:
        bonus += 14
        hits.append("组合:插画型左右分栏")
    elif direction == "上下左右图文" and information_blocks:
        bonus += 30
        hits.append("组合:多信息区块版式")
    elif direction == "上下左右图文" and split_layout:
        bonus += 14
        hits.append("组合:分栏版式")
    return bonus, hits


def classify_external_evidence(
    raw: dict[str, Any],
    min_confidence: float,
    platform: str,
    internal_result: dict[str, Any],
) -> dict[str, Any]:
    """按共享证据判业务线和方向，外部平台不以具体产品作为目录层级。"""
    keywords = normalize_keyword_evidence(raw)
    corpus = keyword_corpus(keywords)
    derived_evidence = has_derived_nested_evidence(keywords)
    image_name = str(raw.get("_image_name", "")).strip()
    evidence_confidence = keywords["evidence_confidence"]
    uncertain_count = len(keywords["uncertain"])
    pure_scenery_poster_evidence = has_text_led_poster_evidence(keywords)
    balanced_text_graphic_evidence = has_balanced_text_graphic_evidence(keywords)
    specific_product = str(internal_result.get("product", ""))
    if specific_product == "待确认":
        specific_product = ""
    filename_product = filename_product_hint(image_name) if image_name else None
    product_for_context = specific_product or filename_product

    business_scores = {
        business_line: score_weighted_rules(corpus, rules)
        for business_line, rules in BUSINESS_LINE_RULES.items()
    }
    mapped_business_line = PRODUCT_BUSINESS_LINES.get(product_for_context or "")
    if mapped_business_line:
        score, hits = business_scores[mapped_business_line]
        business_scores[mapped_business_line] = (
            score + 20,
            hits + [f"产品映射:{product_for_context}"],
        )
    filename_business_line = filename_business_line_hint(image_name, filename_product) if image_name else None
    if filename_business_line:
        score, hits = business_scores[filename_business_line]
        business_scores[filename_business_line] = (
            score + 32,
            hits + [f"文件名业务线:{filename_business_line}"],
        )
    (
        business_line,
        business_confidence,
        business_score,
        business_margin,
        business_hits,
    ) = choose_candidate(
        business_scores,
        evidence_confidence,
        uncertain_count,
        minimum_score=8,
    )
    if filename_business_line and business_line == filename_business_line:
        business_confidence = max(business_confidence, 0.98)
    business_resolved = bool(
        business_line
        and (business_confidence >= min_confidence or business_score >= 14)
    )

    direction: str | None = None
    direction_confidence = 0.0
    direction_score = 0
    direction_margin = 0
    direction_hits: list[str] = []
    direction_scores: dict[str, tuple[int, list[str]]] = {}
    calibrated_candidates: list[dict[str, Any]] = []
    calibrated_matched = 0
    calibrated_margin = 0.0
    calibration_applied = False
    calibration_rejected = ""
    pet_visual_evidence = has_pet_visual_evidence(keywords)
    strong_moon_visual_evidence = has_strong_moon_visual_evidence(keywords)
    strong_illustration_visual_evidence = has_strong_illustration_visual_evidence(keywords)
    sparse_icon_illustration_conflict = has_sparse_icon_illustration_conflict(keywords)
    center_single_icon_evidence = has_center_single_icon_evidence(keywords)
    balanced_split_evidence = has_balanced_text_graphic_evidence(keywords)
    strong_icon_visual_evidence = has_strong_icon_visual_evidence(keywords)
    text_led_poster_evidence = has_text_led_poster_evidence(keywords)
    square_image = is_admuse_square_image(raw, platform)
    monitor_derived_hint = shooting_monitor_derived_hint(keywords)
    ticket_derived_hint = has_ticket_ad_artifact_evidence(keywords)
    media_panel_derived_hint = has_embedded_media_panel_evidence(keywords)
    external_derived_hint = derived_evidence or monitor_derived_hint
    pet_dominant_evidence = has_pet_dominant_evidence(keywords)
    if square_image:
        direction = "朋友圈"
        direction_confidence = max(0.98, evidence_confidence)
        direction_score = 48
        direction_margin = 48
        direction_hits = ["组合:妙思方图统一归朋友圈"]
        direction_scores[direction] = (direction_score, direction_hits)
        calibration_rejected = "妙思方图尺寸规则优先，未使用视觉方向校准"
    elif external_derived_hint:
        direction = GLOBAL_DERIVED_DIRECTION
        direction_confidence = max(0.96, evidence_confidence)
        direction_score = 44
        direction_margin = 44
        direction_hits = [
            "组合:拍摄监视器中的完整保险广告"
            if monitor_derived_hint
            else (
                "组合:完整保险票券置于外层画布"
                if ticket_derived_hint
                else (
                    "组合:内层素材卡+外层品牌评分及二次背景"
                    if media_panel_derived_hint
                    else "组合:完整内层广告+明显外层场景"
                )
            )
        ]
        direction_scores[direction] = (direction_score, direction_hits)
    elif business_line == "保险" and pet_dominant_evidence:
        direction = "宠物"
        direction_confidence = max(0.96, evidence_confidence)
        direction_score = 40
        direction_margin = 40
        direction_hits = ["组合:宠物为第一视觉主体且存在猫狗动物证据"]
        direction_scores[direction] = (direction_score, direction_hits)
        calibration_rejected = "宠物第一视觉主体优先，未使用方向校准模型"
    elif business_line == "保险" and sparse_icon_illustration_conflict:
        direction = "图标"
        direction_confidence = max(0.98, evidence_confidence)
        direction_score = 50
        direction_margin = 50
        direction_hits = ["组合:插画与明确图标证据并存且大面积留白，图标优先"]
        direction_scores[direction] = (direction_score, direction_hits)
        calibration_rejected = "插画图标留白冲突优先，未使用方向校准模型"
    elif business_line == "保险" and strong_illustration_visual_evidence:
        direction = "插画"
        direction_confidence = max(0.96, evidence_confidence)
        direction_score = 40
        direction_margin = 40
        direction_hits = ["组合:人物/家庭插画主体，月亮夜景仅作背景"]
        direction_scores[direction] = (direction_score, direction_hits)
        calibration_rejected = "人物插画主体优先，未使用方向校准模型"
    elif (
        (business_line == "保险" or product_for_context == "百万医疗险")
        and medical_moon_grid_hint(corpus, image_name)
    ):
        direction = "月亮"
        direction_confidence = max(0.96, evidence_confidence)
        direction_score = 40
        direction_margin = 40
        direction_hits = ["组合:保险素材+九宫格月亮布局"]
        direction_scores[direction] = (direction_score, direction_hits)
    elif business_line == "保险" and strong_moon_visual_evidence:
        direction = "月亮"
        direction_confidence = max(0.96, evidence_confidence)
        direction_score = 40
        direction_margin = 40
        direction_hits = ["组合:保险素材+强月亮视觉证据"]
        direction_scores[direction] = (direction_score, direction_hits)
        calibration_rejected = "强月亮视觉证据优先，未使用方向校准模型"
    elif business_line == "保险" and center_single_icon_evidence:
        direction = "图标"
        direction_confidence = max(0.98, evidence_confidence)
        direction_score = 48
        direction_margin = 48
        direction_hits = ["组合:中心单图标主体优先于图文均衡"]
        direction_scores[direction] = (direction_score, direction_hits)
        calibration_rejected = "中心单图标主体优先，未使用方向校准模型"
    elif business_line == "保险" and text_led_poster_evidence:
        direction = "大字报"
        direction_confidence = max(0.96, evidence_confidence)
        direction_score = 40
        direction_margin = 40
        direction_hits = ["组合:文字主导+风景或简单纯色抽象背景"]
        direction_scores[direction] = (direction_score, direction_hits)
        calibration_rejected = "文字主导的简单背景大字报优先，未使用方向校准模型"
    elif business_line == "保险" and balanced_split_evidence:
        direction = "上下左右图文"
        direction_confidence = max(0.96, evidence_confidence)
        direction_score = 40
        direction_margin = 40
        direction_hits = ["组合:API确认图文区域视觉重量均衡"]
        direction_scores[direction] = (direction_score, direction_hits)
        calibration_rejected = "均衡图文版式优先，未使用方向校准模型"
    elif business_line == "保险" and strong_icon_visual_evidence:
        direction = "图标"
        direction_confidence = max(0.96, evidence_confidence)
        direction_score = 40
        direction_margin = 40
        direction_hits = ["组合:大型中心图标或中心环绕图标构图"]
        direction_scores[direction] = (direction_score, direction_hits)
        calibration_rejected = "强图标视觉主体优先，未使用方向校准模型"
    else:
        allowed_external_directions = BUSINESS_LINE_DIRECTIONS.get(business_line or "")
        for external_direction, source_directions in EXTERNAL_DIRECTION_SOURCES.items():
            if allowed_external_directions and external_direction not in allowed_external_directions:
                continue
            combined_rules: list[tuple[str, int]] = []
            for source_direction in source_directions:
                for rule in DIRECTION_RULES.get(source_direction, []):
                    if rule not in combined_rules:
                        combined_rules.append(rule)
            base_score, hits = score_weighted_rules(corpus, combined_rules)
            if external_direction == GLOBAL_DERIVED_DIRECTION and not external_derived_hint:
                base_score, hits = 0, []
            source_bonuses = [
                direction_bonus(source_direction, corpus, product_for_context)
                for source_direction in source_directions
            ]
            shared_bonus, shared_hits = max(source_bonuses, key=lambda value: value[0], default=(0, []))
            external_bonus, external_hits = external_direction_bonus(external_direction, corpus)
            if external_direction == "大字报" and not text_led_poster_evidence:
                direction_scores[external_direction] = (0, [])
            else:
                direction_scores[external_direction] = (
                    base_score + shared_bonus + external_bonus,
                    hits + shared_hits + external_hits,
                )

        ordered_scores = sorted((score for score, _ in direction_scores.values()), reverse=True)
        top_score = ordered_scores[0] if ordered_scores else 0
        second_score = ordered_scores[1] if len(ordered_scores) > 1 else 0
        if top_score < 8 or (second_score > 0 and top_score - second_score <= 3):
            for filename_direction in filename_direction_hints(image_name):
                external_hint = external_direction_name(canonical_direction_name(filename_direction))
                if external_hint not in direction_scores:
                    continue
                if external_hint == "大字报" and not text_led_poster_evidence:
                    continue
                score, hits = direction_scores[external_hint]
                direction_scores[external_hint] = (
                    score + 14,
                    hits + [f"文件名方向:{external_hint}"],
                )
                break
        (
            direction,
            direction_confidence,
            direction_score,
            direction_margin,
            direction_hits,
        ) = choose_candidate(
            direction_scores,
            evidence_confidence,
            uncertain_count,
            minimum_score=8,
        )
    if business_line == "保险":
        (
            _calibrated_direction,
            calibrated_margin,
            calibrated_matched,
            calibrated_candidates,
        ) = calibrated_direction_prediction(keywords)
        audit_only_reason = "校准模型仅供审计，不参与最终分类"
        calibration_rejected = (
            f"{calibration_rejected}；{audit_only_reason}"
            if calibration_rejected
            else audit_only_reason
        )

    direction_resolved = bool(
        direction
        and (direction_confidence >= min_confidence or direction_score >= 14)
    )
    output_business_line = business_line if business_resolved else "待确认"
    output_direction = direction if direction_resolved else ("未知方向" if business_resolved else "待确认")
    # APP 不保留「未知」：方向没定下来就归「内容创意」（业务线本身认不出仍走「待确认」）
    output_direction = apply_app_direction_allowlist(output_direction, output_business_line)
    # 违规拦截是最后一关：命中就改写方向，优先级高于常规判定。
    # 命中即确定违规，所以不再算「仅业务线」（partial）。
    # ⚠️ 必须带上 field_corpora：界面伪装规则用 match_fields 限定只在
    #    ui_mimicry_elements 里匹配，不传就会因为字段语料为空而整条规则失效
    #    （外部平台走的就是这条路径，漏了会出现「内部系统拦得住、妙思拦不住」）。
    violation_hit = match_violation_rule(
        output_business_line,
        corpus,
        uncertain_text_corpus(keywords),
        field_corpora=violation_field_corpora(keywords),
    )
    if violation_hit:
        output_direction = violation_hit[0]
        direction_resolved = True
        direction_hits = [violation_hit[1]]
    abstain = not business_resolved
    partial = bool(business_resolved and not direction_resolved)
    classification_level = "unresolved" if abstain else ("business_line_only" if partial else "complete")
    confidence = business_confidence if partial else (
        min(business_confidence, direction_confidence)
        if business_resolved and direction_resolved
        else 0.0
    )
    if abstain:
        reason = f"系统规则未确定业务线：最高得分 {business_score}，领先 {business_margin}"
    elif partial:
        reason = (
            f"仅确定业务线={output_business_line}；方向未确定："
            f"最高得分 {direction_score}，领先 {direction_margin}；已归档到未知方向"
        )
    else:
        reason = (
            f"系统规则命中：业务线[{','.join(business_hits[:4])}]；"
            f"方向[{','.join(direction_hits[:4])}]"
        )

    result = dict(internal_result)
    result.update({
        "platform": platform,
        "output_mode": "business_line",
        "output_group": output_business_line,
        "specific_product": specific_product,
        "business_line": output_business_line,
        "business_line_confidence": business_confidence,
        "direction": output_direction,
        "direction_confidence": direction_confidence,
        "confidence": confidence,
        "abstain": abstain,
        "partial": partial,
        "classification_level": classification_level,
        "evidence": (business_hits + direction_hits)[:8],
        "reason": reason,
    })
    result["system_scores"] = {
        **internal_result.get("system_scores", {}),
        "business_line": business_score,
        "business_line_margin": business_margin,
        "direction": direction_score,
        "direction_margin": direction_margin,
    }
    result["rule_debug"] = {
        **internal_result.get("rule_debug", {}),
        "platform": platform,
        "output_mode": "business_line",
        "filename_business_line_hint": filename_business_line or "",
        "business_line_candidates": [
            {"name": name, "score": score, "hits": hits}
            for name, (score, hits) in sorted(
                business_scores.items(),
                key=lambda item: item[1][0],
                reverse=True,
            )
        ],
        "direction_candidates": [
            {"name": name, "score": score, "hits": hits}
            for name, (score, hits) in sorted(
                direction_scores.items(),
                key=lambda item: item[1][0],
                reverse=True,
            )
        ],
        "derived_nested_hint": external_derived_hint,
        "shooting_monitor_derived_hint": monitor_derived_hint,
        "ticket_derived_hint": ticket_derived_hint,
        "embedded_media_panel_derived_hint": media_panel_derived_hint,
        "calibrated_direction_model": str(
            DIRECTION_CALIBRATION_MODEL.get("version", "")
        ),
        "calibrated_direction_matched": calibrated_matched,
        "calibrated_direction_margin": round(calibrated_margin, 4),
        "calibrated_direction_candidates": calibrated_candidates,
        "pet_visual_evidence": pet_visual_evidence,
        "pet_dominant_evidence": pet_dominant_evidence,
        "strong_moon_visual_evidence": strong_moon_visual_evidence,
        "strong_illustration_visual_evidence": strong_illustration_visual_evidence,
        "strong_icon_visual_evidence": strong_icon_visual_evidence,
        "center_single_icon_evidence": center_single_icon_evidence,
        "text_led_poster_evidence": text_led_poster_evidence,
        "balanced_split_evidence": balanced_split_evidence,
        "pure_scenery_poster_evidence": text_led_poster_evidence,
        "square_image": square_image,
        "image_width": int(raw.get("_image_width", 0) or 0),
        "image_height": int(raw.get("_image_height", 0) or 0),
        "calibration_applied": calibration_applied,
        "calibration_rejected": calibration_rejected,
    }
    return result


def classify_keyword_evidence(
    raw: dict[str, Any],
    min_confidence: float,
    platform: str = "internal",
) -> dict[str, Any]:
    platform = platform.strip().lower() or "internal"
    if platform not in SUPPORTED_PLATFORMS:
        platform = "internal"
    if platform in SHARED_INSURANCE_DIRECTION_PLATFORMS:
        internal_result = classify_keyword_evidence(raw, min_confidence, "internal")
        external_result = classify_external_evidence(
            raw,
            min_confidence,
            platform,
            internal_result,
        )
        if platform != "reyun" or external_result.get("business_line") == "保险":
            return external_result
        # 热云只在保险业务线上复用外部规则；其他业务仍保留产品目录。
        internal_result["platform"] = platform
        return internal_result

    keywords = normalize_keyword_evidence(raw)
    corpus = keyword_corpus(keywords)
    derived_evidence = has_derived_nested_evidence(keywords)
    image_name = str(raw.get("_image_name", "")).strip()
    filename_product = filename_product_hint(image_name) if image_name else None
    filename_directions = filename_direction_hints(image_name) if image_name else []
    evidence_confidence = keywords["evidence_confidence"]
    uncertain_count = len(keywords["uncertain"])
    pure_scenery_poster_evidence = has_text_led_poster_evidence(keywords)
    balanced_text_graphic_evidence = has_balanced_text_graphic_evidence(keywords)
    center_single_icon_evidence = has_center_single_icon_evidence(keywords)
    strong_moon_visual_evidence = has_strong_moon_visual_evidence(keywords)
    strong_illustration_visual_evidence = has_strong_illustration_visual_evidence(keywords)
    strong_icon_visual_evidence = has_strong_icon_visual_evidence(keywords)
    sparse_icon_illustration_conflict = has_sparse_icon_illustration_conflict(keywords)

    legacy_product_scores = {
        product: score_weighted_rules(
            corpus,
            rules["positive"],
            rules.get("negative", []),
        )
        for product, rules in LEGACY_PRODUCT_RULES.items()
    }
    legacy_product, legacy_product_confidence, legacy_product_score, legacy_product_margin, legacy_product_hits = choose_candidate(
        legacy_product_scores,
        evidence_confidence,
        uncertain_count,
        minimum_score=9,
    )
    conflict = product_conflict(corpus)
    # 无冲突时保留旧产品判定；证据冲突时用完整候选词表做正负分竞争。
    used_legacy_product = legacy_product is not None and conflict is None
    if used_legacy_product:
        product = legacy_product
        product_confidence = legacy_product_confidence
        product_score = legacy_product_score
        product_margin = legacy_product_margin
        product_hits = legacy_product_hits
        product_scores = legacy_product_scores
    else:
        product_scores = {
            candidate_product: score_weighted_rules(
                corpus,
                rules["positive"],
                rules.get("negative", []),
            )
            for candidate_product, rules in PRODUCT_RULES.items()
        }
        # 素材文件名是历史人工整理时留下的弱标签，只在 API 证据尚未达到产品阈值时补分。
        if (
            filename_product
            and conflict is None
            and max((score for score, _ in product_scores.values()), default=0) < 9
        ):
            score, hits = product_scores.get(filename_product, (0, []))
            product_scores[filename_product] = (
                score + 18,
                hits + [f"文件名产品:{filename_product}"],
            )
        product, product_confidence, product_score, product_margin, product_hits = choose_candidate(
            product_scores,
            evidence_confidence,
            uncertain_count,
            minimum_score=9,
        )
    if conflict and product:
        product_hits = product_hits + [f"冲突竞争:{conflict}"]

    overlap_validation = ""
    if product:
        overlap_valid, overlap_validation = validate_overlapping_product(product, corpus, image_name)
        if not overlap_valid:
            product_hits = [f"重叠产品复核失败:{overlap_validation}"]
            product = None
            product_confidence = 0.0
    kuaishou_product_override, kuaishou_logo_evidence = resolve_kuaishou_logo_product(
        keywords,
        filename_product=filename_product,
    )
    if kuaishou_product_override:
        product = kuaishou_product_override
        product_confidence = max(0.98, evidence_confidence)
        product_score = 64
        product_margin = 64
        product_hits = [f"版本复核:{kuaishou_logo_evidence}"]
        product_scores[kuaishou_product_override] = (product_score, product_hits)
        overlap_validation = kuaishou_logo_evidence
        used_legacy_product = False
    if product is None and filename_product in FILENAME_FALLBACK_PRODUCTS:
        # 「命名只能参考」：仅当视觉证据**完全沉默**（一个产品都没判出来）时才用文件名兜底。
        # 视觉证据只要给出了结论，哪怕结论可疑，也不许文件名把它压回去。
        product = filename_product
        product_confidence = max(0.9, evidence_confidence)
        product_score = max(product_score, 40)
        product_margin = max(product_margin, 40)
        product_hits = [f"文件名兜底:{filename_product}"]
        product_scores[filename_product] = (product_score, product_hits)
        overlap_validation = f"文件名兜底:{filename_product}"
        used_legacy_product = False

    direction: str | None = None
    direction_confidence = 0.0
    direction_score = 0
    direction_margin = 0
    direction_hits: list[str] = []
    direction_scores: dict[str, tuple[int, list[str]]] = {}

    if product:
        allowed = PRODUCT_DIRECTIONS[product]
        balanced_internal_direction = next(
            (
                candidate
                for candidate in ("上下左右图文", "左右图文")
                if candidate in allowed
            ),
            "",
        )
        force_global_derived = (
            GLOBAL_DERIVED_DIRECTION in allowed
            and derived_evidence
        )
        force_moon_grid = (
            product == "百万医疗险"
            and "月亮" in allowed
            and medical_moon_grid_hint(corpus, image_name)
        )
        force_strong_moon = bool(
            PRODUCT_BUSINESS_LINES.get(product) == "保险"
            and "月亮" in allowed
            and strong_moon_visual_evidence
        )
        force_sparse_icon_illustration = bool(
            PRODUCT_BUSINESS_LINES.get(product) == "保险"
            and "图标" in allowed
            and sparse_icon_illustration_conflict
        )
        force_pure_scenery_poster = bool(
            PRODUCT_BUSINESS_LINES.get(product) == "保险"
            and "大字报" in allowed
            and pure_scenery_poster_evidence
        )
        force_center_single_icon = bool(
            PRODUCT_BUSINESS_LINES.get(product) == "保险"
            and "图标" in allowed
            and center_single_icon_evidence
            and not force_moon_grid
            and not force_strong_moon
        )
        force_balanced_text_graphic = bool(
            PRODUCT_BUSINESS_LINES.get(product) == "保险"
            and balanced_internal_direction
            and balanced_text_graphic_evidence
        )
        force_unsegmented = (
            "未细分" in allowed
            and not force_global_derived
            and not filename_directions
        )
        legacy_direction: str | None = None
        legacy_direction_confidence = 0.0
        legacy_direction_score = 0
        legacy_direction_margin = 0
        legacy_direction_hits: list[str] = []
        legacy_direction_scores: dict[str, tuple[int, list[str]]] = {}
        if force_global_derived:
            direction = GLOBAL_DERIVED_DIRECTION
            direction_confidence = max(0.96, evidence_confidence)
            direction_score = 44
            direction_margin = 44
            direction_hits = ["组合:产品素材+完整原素材嵌入"]
            direction_scores = {
                GLOBAL_DERIVED_DIRECTION: (direction_score, direction_hits),
            }
        elif force_sparse_icon_illustration:
            direction = "图标"
            direction_confidence = max(0.98, evidence_confidence)
            direction_score = 50
            direction_margin = 50
            direction_hits = ["组合:插画与明确图标证据并存且大面积留白，图标优先"]
            direction_scores = {"图标": (direction_score, direction_hits)}
        elif force_moon_grid:
            direction = "月亮"
            direction_confidence = max(0.96, evidence_confidence)
            direction_score = 40
            direction_margin = 40
            direction_hits = ["组合:医疗险+九宫格月亮布局"]
            direction_scores = {
                "月亮": (direction_score, direction_hits),
            }
        elif force_strong_moon:
            direction = "月亮"
            direction_confidence = max(0.96, evidence_confidence)
            direction_score = 40
            direction_margin = 40
            direction_hits = ["组合:医疗险+强月亮视觉证据"]
            direction_scores = {"月亮": (direction_score, direction_hits)}
        elif force_center_single_icon:
            direction = "图标"
            direction_confidence = max(0.98, evidence_confidence)
            direction_score = 48
            direction_margin = 48
            direction_hits = ["组合:中心单图标主体优先于图文均衡"]
            direction_scores = {"图标": (direction_score, direction_hits)}
        elif force_pure_scenery_poster:
            direction = "大字报"
            direction_confidence = max(0.96, evidence_confidence)
            direction_score = 40
            direction_margin = 40
            direction_hits = ["组合:简单文字覆盖全幅风景背景"]
            direction_scores = {"大字报": (direction_score, direction_hits)}
        elif force_balanced_text_graphic:
            direction = balanced_internal_direction
            direction_confidence = max(0.96, evidence_confidence)
            direction_score = 40
            direction_margin = 40
            direction_hits = ["组合:文字区与图案区视觉重量接近"]
            direction_scores = {direction: (direction_score, direction_hits)}
        elif force_unsegmented:
            direction = "未细分"
            direction_confidence = max(product_confidence, evidence_confidence)
            direction_score = product_score
            direction_margin = product_margin
            direction_hits = ["产品目录未要求方向细分"]
            direction_scores = {"未细分": (direction_score, direction_hits)}
        elif used_legacy_product:
            legacy_allowed = LEGACY_PRODUCT_DIRECTIONS[product]
            if len(legacy_allowed) == 1:
                legacy_direction = legacy_allowed[0]
                legacy_direction_confidence = product_confidence
                legacy_direction_score = product_score
                legacy_direction_margin = product_margin
                legacy_direction_hits = ["产品唯一允许方向"]
                legacy_direction_scores[legacy_direction] = (
                    legacy_direction_score,
                    legacy_direction_hits,
                )
            else:
                for candidate in legacy_allowed:
                    base_score, hits = score_weighted_rules(
                        corpus,
                        LEGACY_DIRECTION_RULES.get(candidate, []),
                    )
                    bonus, bonus_hits = legacy_direction_bonus(candidate, corpus)
                    legacy_direction_scores[candidate] = (base_score + bonus, hits + bonus_hits)
                for filename_direction in filename_directions:
                    filename_direction = canonical_direction_name(filename_direction)
                    if filename_direction not in legacy_direction_scores:
                        continue
                    score, hits = legacy_direction_scores[filename_direction]
                    legacy_direction_scores[filename_direction] = (
                        score + 40,
                        hits + [f"文件名方向:{filename_direction}"],
                    )
                (
                    legacy_direction,
                    legacy_direction_confidence,
                    legacy_direction_score,
                    legacy_direction_margin,
                    legacy_direction_hits,
                ) = choose_candidate(
                    legacy_direction_scores,
                    evidence_confidence,
                    uncertain_count,
                    minimum_score=8,
                )
            if (
                product in {"百万医疗险", "百万防癌险"}
                and any(name in allowed for name in LAYOUT_SUBDIRECTION_NAMES)
                and any(
                    compact_text(word) in corpus
                    for word in ("左右分栏", "左文右图", "左图右文", "右文左图")
                )
                and any(
                    compact_text(word) in corpus
                    for word in (
                        "图标列表", "3d图标主导", "右侧3d图标主体", "对话框文字",
                        "多行条目列表", "左侧文字信息区", "右侧视觉主体区",
                    )
                )
            ):
                # 2026-09-22：原来固定写「上下左右图文」（笼统版式名），改成写细分后的方向。
                # 覆盖的**范围**不变，只是落盘名变具体。
                legacy_direction = refine_layout_direction("上下左右图文", corpus)
                legacy_direction_confidence = max(0.96, evidence_confidence)
                legacy_direction_score = 40
                legacy_direction_margin = 40
                legacy_direction_hits = ["组合:医疗险分栏+图标信息区块"]
                legacy_direction_scores[legacy_direction] = (
                    legacy_direction_score,
                    legacy_direction_hits,
                )
            if (
                product == "QQ阅读"
                and any(name in allowed for name in LAYOUT_SUBDIRECTION_NAMES)
                and (
                    any(
                        compact_text(word) in corpus
                        for word in ("左右分栏", "左文右图", "左图右文", "右文左图")
                    )
                    or (
                        compact_text("复古街机") in corpus
                        and compact_text("上下分栏") in corpus
                    )
                )
            ):
                legacy_direction = refine_layout_direction("上下左右图文", corpus)
                legacy_direction_confidence = max(0.96, evidence_confidence)
                legacy_direction_score = 40
                legacy_direction_margin = 40
                legacy_direction_hits = ["组合:QQ阅读明确分栏版式"]
                legacy_direction_scores[legacy_direction] = (
                    legacy_direction_score,
                    legacy_direction_hits,
                )
            strong_medical_moon = (
                product == "百万医疗险"
                and "月亮" in allowed
                and (
                    any(
                        compact_text(word) in corpus
                        for word in ("发光满月", "发光月亮")
                    )
                    or (
                        compact_text("发光水晶球") in corpus
                        and compact_text("星空背景") in corpus
                    )
                )
            )
            if strong_medical_moon:
                legacy_direction = "月亮"
                legacy_direction_confidence = max(0.96, evidence_confidence)
                legacy_direction_score = 40
                legacy_direction_margin = 40
                legacy_direction_hits = ["组合:医疗险强月相/水晶球星空证据"]
                legacy_direction_scores[legacy_direction] = (
                    legacy_direction_score,
                    legacy_direction_hits,
                )
            strong_medical_illustration = (
                product == "百万医疗险"
                and "插画" in allowed
                and not strong_medical_moon
                and not has_explicit_icon_keyword_evidence(keywords)
                and not has_large_negative_space_evidence(keywords)
                and (
                    (
                        compact_text("卡通插画") in corpus
                        and any(
                            compact_text(word) in corpus
                            for word in ("人物", "女孩", "男孩", "插画主导")
                        )
                    )
                    or (
                        compact_text("翠鸟") in corpus
                        and compact_text("鱼") in corpus
                        and compact_text("上图下文") in corpus
                    )
                )
            )
            if strong_medical_illustration:
                legacy_direction = "插画"
                legacy_direction_confidence = max(0.96, evidence_confidence)
                legacy_direction_score = 40
                legacy_direction_margin = 40
                legacy_direction_hits = ["组合:医疗险明确插画主体"]
                legacy_direction_scores[legacy_direction] = (
                    legacy_direction_score,
                    legacy_direction_hits,
                )
            if (
                product == "快手"
                and "美女" in allowed
                and "美女" in filename_directions
                and any(
                    compact_text(word) in corpus
                    for word in ("女性人物", "美女", "女人")
                )
                and not any(
                    compact_text(word) in corpus
                    for word in ("红包", "金币", "提现", "现金收益", "赚钱", "领现金")
                )
            ):
                legacy_direction = "美女"
                legacy_direction_confidence = max(0.96, evidence_confidence)
                legacy_direction_score = 40
                legacy_direction_margin = 40
                legacy_direction_hits = ["组合:快手女性主体且无收益证据"]
                legacy_direction_scores[legacy_direction] = (
                    legacy_direction_score,
                    legacy_direction_hits,
                )
            if (
                product == "喜番免费短剧"
                and "短剧" in allowed
                and not derived_evidence
                and not any(
                    compact_text(word) in corpus
                    for word in ("提现", "赚钱", "现金收益", "看短剧领金币")
                )
            ):
                legacy_direction = "短剧"
                legacy_direction_confidence = max(0.96, evidence_confidence)
                legacy_direction_score = 40
                legacy_direction_margin = 40
                legacy_direction_hits = ["组合:喜番产品且无网赚/衍生证据"]
                legacy_direction_scores[legacy_direction] = (
                    legacy_direction_score,
                    legacy_direction_hits,
                )

        if (
            legacy_direction
            and external_direction_name(canonical_direction_name(legacy_direction)) == "大字报"
            and not pure_scenery_poster_evidence
        ):
            legacy_direction = None
            legacy_direction_confidence = 0.0
            legacy_direction_score = 0
            legacy_direction_margin = 0
            legacy_direction_hits = ["排除:大字报缺少全幅自然或城市风景背景"]

        # 旧产品和旧方向都足够可靠时直接沿用；否则进入校准方向规则。
        if (
            force_global_derived
            or force_sparse_icon_illustration
            or force_moon_grid
            or force_strong_moon
            or force_center_single_icon
            or force_pure_scenery_poster
            or force_balanced_text_graphic
            or force_unsegmented
        ):
            pass
        elif legacy_direction and legacy_direction_confidence >= min_confidence:
            direction = legacy_direction
            direction_confidence = legacy_direction_confidence
            direction_score = legacy_direction_score
            direction_margin = legacy_direction_margin
            direction_hits = legacy_direction_hits
            direction_scores = legacy_direction_scores
        elif len(allowed) == 1 and used_legacy_product:
            # 兼容旧版“单方向产品”的行为；仅当产品本身已由旧规则可靠确定时使用。
            direction = allowed[0]
            direction_confidence = product_confidence
            direction_score = product_score
            direction_margin = product_margin
            direction_hits = ["产品唯一允许方向"]
            direction_scores[direction] = (direction_score, direction_hits)
        else:
            direction_scores = {}
            for candidate in allowed:
                base_score, hits = score_weighted_rules(
                    corpus,
                    DIRECTION_RULES.get(candidate, []),
                )
                if candidate == GLOBAL_DERIVED_DIRECTION and not derived_evidence:
                    base_score, hits = 0, []
                bonus, bonus_hits = direction_bonus(candidate, corpus, product)
                if (
                    external_direction_name(canonical_direction_name(candidate)) == "大字报"
                    and not pure_scenery_poster_evidence
                ):
                    direction_scores[candidate] = (0, [])
                else:
                    direction_scores[candidate] = (base_score + bonus, hits + bonus_hits)
            # 方向文件名提示只在视觉证据没有过阈值时使用，并且必须属于当前产品的允许方向。
            ordered_direction_scores = sorted(
                (score for score, _ in direction_scores.values()),
                reverse=True,
            )
            top_direction_score = ordered_direction_scores[0] if ordered_direction_scores else 0
            second_direction_score = ordered_direction_scores[1] if len(ordered_direction_scores) > 1 else 0
            filename_tiebreak_needed = (
                top_direction_score < 8
                or (
                    second_direction_score > 0
                    and top_direction_score - second_direction_score <= 3
                )
                )
            if filename_directions and filename_tiebreak_needed:
                for filename_direction in filename_directions:
                    filename_direction = canonical_direction_name(filename_direction)
                    if filename_direction not in direction_scores:
                        continue
                    if (
                        external_direction_name(filename_direction) == "大字报"
                        and not pure_scenery_poster_evidence
                    ):
                        continue
                    score, hits = direction_scores[filename_direction]
                    direction_scores[filename_direction] = (
                        score + 14,
                        hits + [f"文件名方向:{filename_direction}"],
                    )
                    break
            (
                direction,
                direction_confidence,
                direction_score,
                direction_margin,
                direction_hits,
            ) = choose_candidate(
                direction_scores,
                evidence_confidence,
                uncertain_count,
                minimum_score=8,
            )

    # 「上下左右图文」细分成 多区块/左右/上下 图文。放在所有分支收口之前，
    # 这样不论是正常打分、旧规则路径还是版式强制覆盖得到的方向，都会被细分，只改输出名。
    direction = refine_layout_direction(direction, corpus)

    product_name = product or "待确认"
    complete_confidence = min(product_confidence, direction_confidence) if product and direction else 0.0
    direction_resolved = bool(product and direction and complete_confidence >= min_confidence)
    direction_name = canonical_direction_name(direction) if direction_resolved else ("未知方向" if product else "待确认")
    # 只有产品也无法确定时才进入总待确认。产品已确定但方向不明确的素材
    # 保留产品归属，落入“产品/未知方向”，方便后续只复核方向。
    abstain = not product
    partial = bool(product and not direction_resolved)
    classification_level = "unresolved" if abstain else ("product_only" if partial else "complete")
    confidence = product_confidence if partial else complete_confidence
    if abstain:
        if overlap_validation:
            reason = f"系统规则拒绝分类：{overlap_validation}"
        elif conflict:
            reason = (
                f"产品冲突按正负分竞争后仍未确定：{conflict}；"
                f"最高得分 {product_score}，领先 {product_margin}"
            )
        else:
            reason = f"系统规则未确定产品：最高得分 {product_score}，领先 {product_margin}"
    elif partial and not direction:
        reason = (
            f"仅确定产品={product_name}；方向未确定："
            f"最高得分 {direction_score}，领先 {direction_margin}；已归档到未知方向"
        )
    elif partial:
        reason = (
            f"仅确定产品={product_name}；完整分类置信度 {complete_confidence:.2f} "
            f"低于阈值 {min_confidence:.2f}；已归档到未知方向"
        )
    else:
        reason = (
            f"系统规则命中：产品[{','.join(product_hits[:4])}]；"
            f"方向[{','.join(direction_hits[:4])}]"
        )

    api_response_json = {
        key: value
        for key, value in raw.items()
        if not str(key).startswith("_")
    }
    return {
        "platform": platform,
        "output_mode": "product",
        "output_group": product_name,
        "product": product_name,
        "specific_product": product or "",
        "business_line": PRODUCT_BUSINESS_LINES.get(product or "", "待确认"),
        "direction": direction_name,
        "api_evidence_confidence": evidence_confidence,
        "product_confidence": product_confidence,
        "direction_confidence": direction_confidence,
        "confidence": confidence,
        "abstain": abstain,
        "partial": partial,
        "classification_level": classification_level,
        "evidence": (product_hits + direction_hits)[:8],
        "keywords": {
            field: keywords[field]
            for field in (
                *KEYWORD_PROMPT_FIELDS[:-1],
                *STRUCTURED_PROMPT_FIELDS,
                *OPTIONAL_STRUCTURED_PROMPT_FIELDS,
                "uncertain",
            )
        },
        "system_scores": {
            "product": product_score,
            "product_margin": product_margin,
            "direction": direction_score,
            "direction_margin": direction_margin,
        },
        "rule_debug": {
            "keyword_corpus": corpus,
            "image_name": image_name,
            "filename_product_hint": filename_product or "",
            "filename_direction_hints": filename_directions,
            "derived_nested_hint": bool(product and derived_evidence),
            "medical_moon_grid_hint": bool(product == "百万医疗险" and medical_moon_grid_hint(corpus, image_name)),
            "strong_moon_visual_evidence": strong_moon_visual_evidence,
            "strong_illustration_visual_evidence": strong_illustration_visual_evidence,
            "strong_icon_visual_evidence": strong_icon_visual_evidence,
            "explicit_icon_keyword_evidence": has_explicit_icon_keyword_evidence(keywords),
            "large_negative_space_evidence": has_large_negative_space_evidence(keywords),
            "sparse_icon_illustration_conflict": sparse_icon_illustration_conflict,
            "product_min_score": 9,
            "direction_min_score": 8,
            "pure_scenery_poster_evidence": pure_scenery_poster_evidence,
            "balanced_text_graphic_evidence": balanced_text_graphic_evidence,
            "center_single_icon_evidence": center_single_icon_evidence,
            "min_confidence": min_confidence,
            "conflict": conflict or "",
            "conflict_resolution": "正负分竞争" if conflict else "",
            "overlap_validation": overlap_validation,
            "kuaishou_logo_variant": keywords.get("kuaishou_logo_variant", ""),
            "kuaishou_logo_product_override": kuaishou_product_override or "",
            "kuaishou_logo_evidence": kuaishou_logo_evidence,
            "product_candidates": [
                {"name": name, "score": score, "hits": hits}
                for name, (score, hits) in sorted(
                    product_scores.items(),
                    key=lambda item: item[1][0],
                    reverse=True,
                )
            ],
            "direction_candidates": [
                {"name": name, "score": score, "hits": hits}
                for name, (score, hits) in sorted(
                    direction_scores.items(),
                    key=lambda item: item[1][0],
                    reverse=True,
                )
            ],
        },
        "api_response_raw": str(
            raw.get("_api_raw_text")
            or json.dumps(api_response_json, ensure_ascii=False)
        ),
        "api_response_json": api_response_json,
        "api_meta": raw.get("_api_meta", {}),
        "prompt_version": PROMPT_VERSION,
        "reason": reason,
        "cached": keywords["cached"],
    }


# 引擎内部产出的名字 → 策略中心的名字。策略中心是业务目录的权威来源，
# 落盘目录要跟它一致（见 scripts/taxonomy_overrides.json）。
COLLECTION_PRODUCT_ALIASES = {
    "中国移动APP-签到有礼+10元话费": "移动APP",
    "QQ阅读": "QQ阅读-25元",
}
COLLECTION_DIRECTION_ALIASES = {
    "未知方向": "未知",
    "待确认": "未知",
    "未细分": "未知",
    "宠物": "萌宠",
    "旅游攻略": "旅游",
    "美女网赚": "网赚",
    "文字大字报": "大字报",
    # 策略中心对 QQ阅读-25元 用「小程序诱导」，本地点叫「小程序／诱导类」，同一件事
    "小程序": "小程序诱导",
    "诱导类": "小程序诱导",
    # 业务侧 2026-09-22 明确：**购物就是低价好物**（策略中心用「购物」，本地点用「低价好物」）
    "低价好物": "购物",
    # 业务侧 2026-09-28 明确：**电商并入快手电商**，招商/电商经营类只留这一个方向。
    # 校准模型的标签里仍有「电商」，不走这条别名它会掉到清单外（→ 内容创意）。
    "电商": "快手电商",
}
COLLECTION_DERIVED_DIRECTIONS = {
    "自动衍生",
    "妙思自动衍生",
    "秒思自动衍生",
    "妙思衍生",
}
COLLECTION_DEFAULT_PRODUCTS = {
    "保险": "百万医疗险",
    "卡券": "QQ阅读-25元",
}
COLLECTION_UNRESOLVED_PRODUCTS = {"", "待确认"}


def has_clear_poster_evidence(result: dict[str, Any]) -> bool:
    keywords = result.get("keywords", {})
    if not isinstance(keywords, dict):
        return False
    corpus = keyword_corpus(keywords)
    rule_debug = result.get("rule_debug", {})
    image_name = compact_text(str(rule_debug.get("image_name", ""))) if isinstance(rule_debug, dict) else ""
    explicit_markers = (
        "广告海报", "宣传海报", "营销海报", "竖版海报", "横版海报",
        "整版海报", "海报式设计", "海报版式", "主视觉海报",
    )
    if any(compact_text(marker) in corpus or compact_text(marker) in image_name for marker in explicit_markers):
        return True
    layout_corpus = compact_text("|".join(str(value) for value in keywords.get("layout_keywords", [])))
    single_canvas = keywords.get("canvas_layer") in ("", None, "单层画布")
    structured_layout = keywords.get("composition_structure") in {
        "左右图文", "上下图文", "多区块图文",
    }
    visual_subject = keywords.get("graphic_type") in {
        "插画", "人物照片", "风景照片", "产品照片", "其他",
    }
    title_marker = any(
        compact_text(marker) in layout_corpus
        for marker in ("大号标题", "主标题", "宣传标题", "标题区", "标题文字")
    )
    information_marker = any(
        compact_text(marker) in layout_corpus
        for marker in ("卖点列表", "权益列表", "信息卡片", "信息区", "产品信息", "多区块图文")
    )
    return bool(
        single_canvas
        and structured_layout
        and visual_subject
        and title_marker
        and information_marker
        and meaningful_marketing_text_count(keywords) >= 2
    )


def apply_collection_taxonomy(result: dict[str, Any]) -> dict[str, Any]:
    output = copy.deepcopy(result)
    taxonomy_products = COLLECTION_TAXONOMY.get("products", {})
    source_product = str(
        output.get("specific_product") or output.get("product") or ""
    ).strip()
    source_product_unresolved = source_product in COLLECTION_UNRESOLVED_PRODUCTS
    taxonomy_product = COLLECTION_PRODUCT_ALIASES.get(source_product, source_product)
    source_business_line = str(
        output.get("business_line") or output.get("output_group") or ""
    ).strip()
    if taxonomy_product in COLLECTION_UNRESOLVED_PRODUCTS:
        taxonomy_product = COLLECTION_DEFAULT_PRODUCTS.get(source_business_line, "")

    original_source_direction = str(output.get("direction") or "待确认").strip()
    source_direction = original_source_direction
    keywords = output.get("keywords", {})
    if not isinstance(keywords, dict):
        keywords = {}
    calibrated_direction: str | None = None
    calibrated_probability = 0.0
    calibrated_margin = 0.0
    calibrated_matched = 0
    calibrated_candidates: list[dict[str, Any]] = []
    calibration_applied = False
    calibration_reason = ""
    platform = str(output.get("platform") or "").strip().lower()
    app_calibration_scope = bool(
        source_business_line == "APP"
        or taxonomy_product in {"快手", "快手极速版"}
        or (
            platform in EXTERNAL_BUSINESS_PLATFORMS
            and source_product_unresolved
            and source_business_line in {"", "APP", "待确认"}
        )
    )
    if app_calibration_scope and original_source_direction not in COLLECTION_DERIVED_DIRECTIONS:
        strong_direction, strong_reason = strong_app_direction(keywords, taxonomy_product)
        (
            calibrated_direction,
            calibrated_probability,
            calibrated_margin,
            calibrated_matched,
            calibrated_candidates,
        ) = calibrated_app_direction_prediction(keywords, original_source_direction)
        if strong_direction:
            source_direction = strong_direction
            calibration_applied = True
            calibration_reason = f"APP明确语义规则:{strong_reason}"
        elif (
            calibrated_direction
            and calibrated_matched >= int(APP_DIRECTION_CALIBRATION_MODEL.get("minimum_matched", 2))
            and calibrated_margin >= float(APP_DIRECTION_CALIBRATION_MODEL.get("minimum_margin", 0.75))
            and calibrated_probability >= float(
                APP_DIRECTION_CALIBRATION_MODEL.get("minimum_probability", 0.38)
            )
        ):
            source_direction = calibrated_direction
            calibration_applied = True
            calibration_reason = (
                f"APP人工校准模型:{calibrated_direction}"
                f"(命中{calibrated_matched},概率{calibrated_probability:.3f},间隔{calibrated_margin:.3f})"
            )
    normalized_app_direction = normalize_app_direction_for_product(
        source_direction,
        source_product,
    )
    if normalized_app_direction != source_direction:
        source_direction = normalized_app_direction
        calibration_applied = True
        calibration_reason = f"{calibration_reason}；产品未知时快手电商归一为电商".lstrip("；")
    poster_evidence = source_direction in {"上下左右图文", "左右图文"} and has_clear_poster_evidence(output)
    if source_direction in COLLECTION_DERIVED_DIRECTIONS:
        taxonomy_direction = str(COLLECTION_TAXONOMY["derived_direction"])
    elif poster_evidence:
        taxonomy_direction = "海报"
    elif taxonomy_product == "百度网盘" and source_direction == "未细分":
        taxonomy_direction = "常规"
    else:
        canonical_direction = canonical_direction_name(source_direction)
        taxonomy_direction = COLLECTION_DIRECTION_ALIASES.get(
            canonical_direction,
            canonical_direction,
        )

    taxonomy_found = taxonomy_product in taxonomy_products
    if taxonomy_found:
        product_spec = taxonomy_products[taxonomy_product]
        allowed_directions = list(product_spec.get("directions", []))
        output_group = str(product_spec.get("group") or "未知")
        if taxonomy_direction not in allowed_directions:
            taxonomy_direction = str(COLLECTION_TAXONOMY["unknown_direction"])
    else:
        allowed_directions = []
        output_group = "未知"
        taxonomy_product = "未知"
        calibrated_unknown_direction = bool(
            source_product_unresolved
            and calibration_applied
            and taxonomy_direction not in {
                "",
                "待确认",
                "未知方向",
                str(COLLECTION_TAXONOMY["unknown_direction"]),
            }
        )
        if not calibrated_unknown_direction:
            taxonomy_direction = str(COLLECTION_TAXONOMY["unknown_direction"])

    # APP 业务线收敛到业务侧确认的方向清单：门控成「未知」的、或落在清单外的，
    # 一律归「内容创意」（2026-09-28 业务侧定调，与 business_line 模式保持一致）。
    taxonomy_direction = apply_app_direction_allowlist(taxonomy_direction, source_business_line)

    # 违规拦截是最后一关，与 business_line 路径保持一致 ——
    # 同一个素材不能因为走的平台不同而落到不同方向。
    violation_hit = match_violation_rule(
        source_business_line,
        keyword_corpus(keywords),
        uncertain_text_corpus(keywords),
        field_corpora=violation_field_corpora(keywords),
    )
    violation_reason = ""
    if violation_hit:
        taxonomy_direction = violation_hit[0]
        violation_reason = violation_hit[1]

    output.update({
        "output_mode": "collection_taxonomy",
        "output_group": output_group,
        "output_product": taxonomy_product,
        "product": taxonomy_product,
        "detected_product": source_product or "待确认",
        "detected_direction": source_direction,
        "direction": taxonomy_direction,
        "taxonomy_version": str(COLLECTION_TAXONOMY.get("version", "")),
        "abstain": False,
        "partial": taxonomy_direction == COLLECTION_TAXONOMY["unknown_direction"],
        "classification_level": (
            "product_only"
            if taxonomy_direction == COLLECTION_TAXONOMY["unknown_direction"]
            else "complete"
        ),
    })
    gate_reason = (
        f"分类表门控：{source_product or '待确认'}/{source_direction}"
        f" → {output_group}/{taxonomy_product}/{taxonomy_direction}"
    )
    output["reason"] = f"{str(output.get('reason') or '').strip()}；{gate_reason}".lstrip("；")
    evidence = list(output.get("evidence", []))
    evidence.append(gate_reason)
    if violation_reason:
        # 违规拦截是最后一关，必须写进审计，否则看不出方向是被谁改的
        output["reason"] = f"{output['reason']}；违规拦截：{violation_reason}"
        evidence.append(f"违规拦截：{violation_reason}")
    output["evidence"] = evidence[:9]
    existing_debug = output.get("rule_debug", {})
    output["rule_debug"] = {
        **(existing_debug if isinstance(existing_debug, dict) else {}),
        "taxonomy_version": output["taxonomy_version"],
        "taxonomy_source_product": source_product,
        "taxonomy_product": taxonomy_product,
        "taxonomy_group": output_group,
        "taxonomy_original_source_direction": original_source_direction,
        "taxonomy_source_direction": source_direction,
        "taxonomy_direction": taxonomy_direction,
        "taxonomy_allowed_directions": allowed_directions,
        "taxonomy_poster_evidence": poster_evidence,
        "app_direction_calibration_model": str(
            APP_DIRECTION_CALIBRATION_MODEL.get("version", "")
        ),
        "app_direction_calibration_scope": app_calibration_scope,
        "app_direction_calibration_applied": calibration_applied,
        "app_direction_calibration_reason": calibration_reason,
        "app_direction_calibration_probability": round(calibrated_probability, 4),
        "app_direction_calibration_margin": round(calibrated_margin, 4),
        "app_direction_calibration_matched": calibrated_matched,
        "app_direction_calibration_candidates": calibrated_candidates,
        "taxonomy_gate_applied": True,
    }
    return output


def classification_destination(
    output: Path,
    result: dict[str, Any],
    orientation: str = "",
) -> Path:
    """按分类表层级选择目录，API 失败仍进入总待确认。

    层级：产品 [/ 二级产品] [/ 方向] [/ 横版|竖版]。
    横竖由调用方按图片真实尺寸算好传进来（见 image_orientation）；
    不传则保持原行为，方便回放类工具按需选择。
    """
    if result.get("abstain"):
        destination = output / "_待确认"
    else:
        destination = output / str(result.get("output_group") or result["product"])
        output_product = str(result.get("output_product") or "").strip()
        if output_product:
            destination /= output_product
        direction = str(result["direction"])
        if direction != "未细分":
            destination /= direction
    return destination / orientation if orientation else destination


def place_file(src: Path, dest_dir: Path, action: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    destination = dest_dir / src.name
    counter = 1
    while destination.exists():
        destination = dest_dir / f"{src.stem}_{counter}{src.suffix}"
        counter += 1
    if action == "copy":
        shutil.copy2(src, destination)
    else:
        shutil.move(str(src), str(destination))
    return destination


def is_inside(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def scan_images(source: Path, output: Path) -> list[Path]:
    return sorted(
        path for path in source.rglob("*")
        if path.is_file()
        and path.suffix.lower() in IMAGE_SUFFIXES
        and not is_inside(path.resolve(), output)
    )


def image_checkpoint_key(image: Path) -> str:
    stat = image.stat()
    return f"{image.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"


def checkpoint_destination(entry: dict[str, Any], output: Path) -> Path | None:
    raw = str(entry.get("destination", "")).strip()
    if not raw:
        return None
    destination = Path(raw)
    if destination.is_file():
        return destination
    parts = destination.parts
    for index, part in enumerate(parts):
        if part == ".重新分类工作区" or part.startswith(".重新分类临时-"):
            candidate = output.joinpath(*parts[index + 1:])
            return candidate if candidate.is_file() else None
    return None


def load_completed_checkpoints(audit_path: Path, output: Path) -> dict[str, dict[str, Any]]:
    if not audit_path.is_file():
        return {}
    completed: dict[str, dict[str, Any]] = {}
    try:
        lines = audit_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in lines:
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict):
                continue
            source = Path(str(entry.get("source", "")))
            key = str(entry.get("source_key", "")).strip()
            if not key and source.is_file():
                key = image_checkpoint_key(source)
            destination = checkpoint_destination(entry, output)
            api_failed = bool(entry.get("api_failed")) or (
                destination is not None and destination.parent.name == "API失败"
            )
            if api_failed:
                if destination is not None:
                    destination.unlink(missing_ok=True)
                completed.pop(key, None)
            elif key and destination is not None:
                entry["destination"] = str(destination)
                completed[key] = entry
        except (OSError, ValueError, TypeError):
            continue
    return completed


def write_audit(
    handle,
    image: Path,
    result: dict[str, Any],
    status: str,
    destination: Path,
    source_key: str,
) -> None:
    entry = {
        "file": image.name,
        "source": str(image),
        # 移动模式下源文件在写审计前已经离开原路径，必须使用移动前保存的键。
        "source_key": source_key,
        "status": status,
        "destination": str(destination),
        **result,
    }
    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    handle.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="API 极速分类器")
    parser.add_argument("--print-prompt", action="store_true", help="输出内置提示词并退出")
    parser.add_argument("--print-ui-config", action="store_true", help="输出界面可编辑的默认配置并退出")
    parser.add_argument("--validate-ui-config", help="校验界面保存的分类配置并退出")
    parser.add_argument("--source", help="图片源目录")
    parser.add_argument("--output", help="输出目录")
    parser.add_argument("--config", help="API配置文件路径")
    parser.add_argument(
        "--platform",
        choices=sorted(SUPPORTED_PLATFORMS),
        default="internal",
        help="采集平台；所有平台最终按分类表输出一级分类/产品/方向",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=10,
        help="API并发数；实测该网关 5→10 吞吐提升 1.55x，16 反而更慢，10 是拐点",
    )
    parser.add_argument("--min-confidence", type=float, default=0.85, help="最低置信度阈值")
    parser.add_argument("--action", choices=["copy", "move"], default="copy", help="文件动作")
    parser.add_argument("--refresh-api", action="store_true", help="跳过视觉 API 缓存并重新识别")
    args = parser.parse_args()

    if args.print_prompt:
        print(PROMPT)
        return
    if args.print_ui_config:
        print(json.dumps(classification_ui_defaults(), ensure_ascii=False))
        return
    if args.validate_ui_config:
        try:
            candidate_path = Path(args.validate_ui_config).resolve()
            candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
            normalized = validate_classification_ui_config(candidate)
            print(json.dumps({
                "ok": True,
                "prompt_length": len(normalized["api_classify_prompt"]),
                "rule_sections": sorted(normalized["classification_rules"]),
            }, ensure_ascii=False))
            return
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            raise SystemExit(2) from None
    if not args.source or not args.output or not args.config:
        parser.error("--source、--output 和 --config 为必填参数")

    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    config_path = Path(args.config).resolve()
    parallel = max(1, min(10, args.parallel))
    min_confidence = max(0.0, min(1.0, args.min_confidence))

    if not source.is_dir():
        eprint(f"源目录不存在：{source}")
        raise SystemExit(1)
    if not config_path.is_file():
        eprint(f"配置文件不存在：{config_path}")
        raise SystemExit(1)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    apply_classification_rule_overrides(config.get("classification_rules", {}))
    output.mkdir(parents=True, exist_ok=True)
    cache_dir = config_path.parent / "cache" / "api_fast_classify" / PROMPT_VERSION
    images = scan_images(source, output)
    total = len(images)
    audit_path = output / "_API分类审计.jsonl"
    audit_excel_path = output / "_API分类调试审计.xlsx"
    completed = load_completed_checkpoints(audit_path, output)
    image_keys = {image: image_checkpoint_key(image) for image in images}
    pending_images = [image for image in images if image_keys[image] not in completed]
    resumed_count = total - len(pending_images)

    emit(f"FOUND|{total}")
    if total == 0:
        eprint("未找到图片")
        return

    api_mode = "重新调用 API 提取关键词" if args.refresh_api else "AI提取关键词"
    emit(f"STAGE|API|READY|{api_mode} · 本地规则判定 · {parallel}路并发")
    if resumed_count:
        emit(f"RESUME|{resumed_count}|{total}")
        eprint(f"断点续跑：已完成 {resumed_count} 张，本次剩余 {len(pending_images)} 张")
    if args.platform == "reyun":
        output_mode = "业务线/方向（仅保险业务线；其余仍为 产品/方向）"
    elif args.platform in EXTERNAL_BUSINESS_PLATFORMS:
        output_mode = "业务线/方向"
    else:
        output_mode = "一级分类/产品/方向（表格分类）"
    eprint(f"平台 {args.platform} · 输出模式 {output_mode}")
    eprint(
        f"找到 {total} 张图片 · {api_mode} · 系统规则决定分类 · 并发 {parallel} · "
        f"最低置信度 {min_confidence:.2f} · 文件动作 {args.action}"
    )

    auto_count = sum(1 for entry in completed.values() if entry.get("status") == "AUTO")
    partial_count = sum(1 for entry in completed.values() if entry.get("partial"))
    review_count = resumed_count - auto_count
    with audit_path.open("a", encoding="utf-8") as audit:
        with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="api-classify") as executor:
            futures = {
                executor.submit(
                    call_vision_api,
                    image,
                    config,
                    cache_dir,
                    refresh_api=args.refresh_api,
                ): image
                for image in pending_images
            }
            for current_run_done, future in enumerate(as_completed(futures), 1):
                done = resumed_count + current_run_done
                image = futures[future]
                source_key = image_keys[image]
                destination: Path | None = None
                emit(f"API_PROGRESS|{done}|{total}|{image.name}")
                try:
                    api_result = future.result()
                    api_result["_image_name"] = image.name
                    image_width, image_height = read_image_dimensions(image)
                    api_result["_image_width"] = image_width
                    api_result["_image_height"] = image_height
                    classified = classify_keyword_evidence(
                        api_result, min_confidence, args.platform
                    )
                    # 按引擎自己声明的输出模式分流：
                    #   business_line —— 外部平台（妙思/有米云，以及热云的保险业务线）
                    #                    输出「业务线/方向」，不再强行套 12 个产品。
                    #                    以前无条件套分类表，导致 APP/小卡/存量 这些
                    #                    业务线的素材产品全落「未知」（实测一个妙思批次
                    #                    500 张里 229 张未知产品）。
                    #   product        —— 内部系统，继续走「一级分类/产品/方向」分类表门控。
                    if classified.get("output_mode") == "business_line":
                        result = classified
                    else:
                        result = apply_collection_taxonomy(classified)
                    result["api_refresh_requested"] = args.refresh_api
                    output_group = str(result.get("output_group") or "未知")
                    output_product = str(result.get("output_product") or "").strip()
                    # 业务线模式没有产品层级，别退回 result["product"] 拼出误导性的两级名
                    product = f"{output_group}/{output_product}" if output_product else output_group
                    direction = result["direction"]
                    if result["abstain"]:
                        status = "REVIEW"
                    else:
                        status = "AUTO"

                    destination_dir = classification_destination(
                        output, result, image_orientation(image)
                    )
                    destination = place_file(image, destination_dir, args.action)
                    write_audit(audit, image, result, status, destination, source_key)
                    if status == "REVIEW":
                        review_count += 1
                    else:
                        auto_count += 1
                        if result["partial"]:
                            partial_count += 1
                    emit(f"PROGRESS|{done}|{total}|{status}|{product}|{direction}|{image.name}")
                    cache_label = " · 缓存" if result["cached"] else ""
                    eprint(
                        f"[{done}/{total}] {image.name} → {product}/{direction} "
                        f"({result['confidence']:.2f}) "
                        f"{'仅产品' if result.get('partial') else ('✓' if status == 'AUTO' else '待确认')}{cache_label}"
                    )
                except Exception as exc:
                    eprint(f"[{done}/{total}] {image.name} 处理失败：{exc}")
                    # API/规则失败时源文件仍在，才能转入 API失败；如果文件已经
                    # 成功归档，绝不能再次移动同一源路径，否则会掩盖真正异常。
                    if destination and destination.is_file():
                        raise RuntimeError(
                            f"分类后处理失败；文件已归档到 {destination}，不再重复移动：{exc}"
                        ) from exc
                    if not image.is_file():
                        raise RuntimeError(f"分类后处理失败且源文件已不存在：{exc}") from exc
                    destination = place_file(image, output / "_待确认" / "API失败", args.action)
                    failed_result = {
                        "product": "待确认",
                        "direction": "待确认",
                        "confidence": 0.0,
                        "abstain": True,
                        "api_failed": True,
                        "api_refresh_requested": args.refresh_api,
                        "reason": str(exc)[:500],
                    }
                    write_audit(audit, image, failed_result, "REVIEW", destination, source_key)
                    review_count += 1
                    emit(f"PROGRESS|{done}|{total}|REVIEW|API失败|待确认|{image.name}")

    try:
        export_api_audit_xlsx(
            audit_path,
            audit_excel_path,
            {
                "source": str(source),
                "output": str(output),
                "model": str(config.get("model_id", "")),
                "prompt_version": PROMPT_VERSION,
                "min_confidence": min_confidence,
                "parallel": parallel,
                "action": args.action,
                "platform": args.platform,
            },
        )
        emit(f"AUDIT_EXCEL|{audit_excel_path}")
    except Exception as exc:
        eprint(f"生成调试审计 Excel 失败：{exc}")

    emit(f"SUMMARY|AUTO={auto_count}|REVIEW={review_count}|TOTAL={total}")
    emit(f"OUTPUT|{output}")
    eprint("")
    eprint("=== 分类完成 ===")
    eprint(f"自动分类: {auto_count} ({100 * auto_count / total:.1f}%)")
    eprint(f"其中仅确定产品、归入未知方向: {partial_count}")
    eprint(f"待确认: {review_count}")
    eprint(f"分类审计: {audit_path}")
    if audit_excel_path.is_file():
        eprint(f"调试审计 Excel: {audit_excel_path}")
    eprint(f"输出目录: {output}")


if __name__ == "__main__":
    main()
