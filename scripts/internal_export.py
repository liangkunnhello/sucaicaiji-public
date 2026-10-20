import os
import time
import json
import re
import requests
import pandas as pd
import subprocess
import sys
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
from output_layout import get_batch_layout, write_capture_items

# Windows 终端默认可能使用 GBK；采集日志包含中文和 ✓/✗ 状态符号，
# 统一采用 UTF-8，避免错误处理分支又被 UnicodeEncodeError 覆盖真实原因。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

KNOWN_BUSINESS_LINES = ['保险', 'APP', '卡券', '小卡', '存量', '未知']

CDP_ENDPOINT = os.environ.get("MATERIAL_DOWNLOADER_CDP_ENDPOINT", "http://127.0.0.1:9223").rstrip("/")
# 内部系统地址：不同环境部署在自己的域名上，用环境变量注入，不要写死在代码里。
INTERNAL_URL = os.environ.get(
    "MATERIAL_DOWNLOADER_INTERNAL_URL",
    "https://internal-center.example.com/login#/home/video-material-consumption",
)
# 判定「这是不是内部系统页面」用的域名后缀，从上面的地址自动推导。
INTERNAL_URL_HOST = urlparse(INTERNAL_URL).hostname or ""
INTERNAL_HOST_SUFFIX = ".".join(INTERNAL_URL_HOST.split(".")[-2:]) if INTERNAL_URL_HOST else ""
BACKGROUND_MODE = os.environ.get("MATERIAL_DOWNLOADER_BACKGROUND_MODE", "true").strip().lower() not in {"0", "false", "no", "off"}
FILTER_DATE = os.environ.get("MATERIAL_DOWNLOADER_FILTER_DATE", "").strip()
if FILTER_DATE and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", FILTER_DATE):
    raise RuntimeError(f"内部系统筛选日期格式无效：{FILTER_DATE}")

# 视频模式：内部系统 API 直采（产品关键词 + 视频消耗 Top N）
INTERNAL_MATERIAL_TYPE = os.environ.get("MATERIAL_DOWNLOADER_INTERNAL_MATERIAL_TYPE", "").strip()
INTERNAL_KEYWORD = os.environ.get("MATERIAL_DOWNLOADER_INTERNAL_KEYWORD", "").strip()
try:
    INTERNAL_TOP_N = max(1, int(os.environ.get("MATERIAL_DOWNLOADER_INTERNAL_TOP_N", "50") or 50))
except ValueError:
    INTERNAL_TOP_N = 50


def maybe_bring_to_front(page):
    if not BACKGROUND_MODE:
        page.bring_to_front()


def select_filter_date(page, filter_date=""):
    """选择内部系统查询日期；重试时必须精确复用原日期。

    ⚠ 不能只写 input.value：Element 的 daterange 控件把「显示值」和「内部模型」
    分开存，只改显示时查询仍用页面加载时的旧日期，历史日期重跑会稳定失败。
    必须直接驱动 ElDatePicker 组件（emitInput 会同时更新 v-model 与显示值），
    与 internal_export_cdp.set_filter_date 保持同一套做法。
    """
    if not filter_date:
        import datetime as _datetime

        filter_date = (_datetime.date.today() - _datetime.timedelta(days=1)).isoformat()
        print(f"选择时间范围为“昨天”（{filter_date}）...")
    else:
        print(f"选择原任务筛选日期：{filter_date}...")

    page.evaluate(
        """(target) => {
            const editor = document.querySelector(".el-date-editor--daterange")
                || [...document.querySelectorAll(".el-date-editor")]
                     .find(element => element.querySelector("input[placeholder='开始日期']"));
            if (!editor) return;
            const parts = target.split("-").map(Number);
            const date = new Date(parts[0], parts[1] - 1, parts[2]);
            const vm = editor.__vue__;
            if (vm && typeof vm.emitInput === "function") {
                vm.emitInput([date, date]);
            } else if (vm) {
                vm.$emit("input", [date, date]);
            }
        }""",
        filter_date,
    )
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)
    values = [
        value.strip()
        for value in page.evaluate(
            "() => [...document.querySelectorAll('.el-range-input')].map(input => input.value)"
        )
    ]
    if values != [filter_date, filter_date]:
        raise RuntimeError(f"内部系统筛选日期设置失败：期望 {filter_date}，页面显示 {values}")
    print(f"✓ 已固定查询日期：{filter_date} 至 {filter_date}")


def clear_product_filter(page):
    """清除「产品」筛选中残留的已选项。

    账号管家标签页是长期存活的浏览器上下文，产品筛选不会被上一轮任务重置；
    残留产品在查询日期没有消耗数据时接口稳定返回 0 条，导出空表后表现为
    “本次没有采集到素材”。
    """
    try:
        removed = page.evaluate(
            """() => {
                const visible = element => Boolean(
                    element.offsetWidth || element.offsetHeight || element.getClientRects().length
                );
                const text = element => (element.innerText || element.textContent || "").trim();
                const item = [...document.querySelectorAll(".el-form-item")]
                    .filter(visible)
                    .find(element => {
                        const label = element.querySelector(".el-form-item__label");
                        return label && text(label) === "产品";
                    });
                if (!item) return null;
                const tags = [...item.querySelectorAll(".el-tag")].filter(visible);
                const names = tags.map(text);
                for (const tag of tags) {
                    const close = tag.querySelector(".el-tag__close");
                    if (close) close.click();
                }
                return names;
            }"""
        )
    except Exception as error:
        print("清理产品筛选失败:", error)
        return []
    if removed is None:
        print("  ⚠ 未找到「产品」筛选项，跳过残留筛选清理")
        return []
    if removed:
        print(f"✓ 已清除残留的产品筛选：{'、'.join(removed)}")
    return removed


def read_result_total(page):
    """读取查询结果总条数；取不到时返回 None。"""
    try:
        return page.evaluate(
            """() => {
                const visible = element => Boolean(
                    element.offsetWidth || element.offsetHeight || element.getClientRects().length
                );
                const node = [...document.querySelectorAll(".el-pagination__total")]
                    .filter(visible)[0];
                if (!node) return null;
                const match = (node.innerText || node.textContent || "").match(/(\\d+)/);
                return match ? Number(match[1]) : null;
            }"""
        )
    except Exception:
        return None


EXPORT_ROOT = os.environ.get("MATERIAL_DOWNLOADER_EXPORT_DIR", r"D:\跑量素材\外部跑量素材")
OUTPUT_LAYOUT = get_batch_layout("内部系统", EXPORT_ROOT)
INTERNAL_BATCH_DIR = str(OUTPUT_LAYOUT["batch_dir"])
INTERNAL_IMAGE_DIR = str(OUTPUT_LAYOUT["raw_dir"])
INTERNAL_REPORT_DIR = str(OUTPUT_LAYOUT["report_dir"])
INTERNAL_SAVE_DIR = INTERNAL_IMAGE_DIR


def check_and_start_browser():
    """Ensure the managed browser exposes the CDP endpoint before export.

    Keep this compatibility wrapper because the legacy Playwright path calls
    it, but delegate the actual probe/recovery to ``cdp_guard`` so direct and
    fallback export paths have identical behaviour and diagnostics.
    """
    try:
        from cdp_guard import ensure_cdp, get_cdp_endpoint
        if ensure_cdp():
            global CDP_ENDPOINT
            CDP_ENDPOINT = get_cdp_endpoint()
            print("✓ 账号管家调试端口 9223 已就绪")
            return True
    except ImportError:
        # A legacy standalone copy may not contain cdp_guard yet.  Preserve a
        # bounded probe instead of silently claiming the browser is ready.
        try:
            resp = requests.get(f"{CDP_ENDPOINT}/json/version", timeout=3)
            if resp.ok and resp.json().get("webSocketDebuggerUrl"):
                print("✓ 账号管家调试端口 9223 已开启")
                return True
        except Exception:
            pass
    except Exception as error:
        print(f"✗ 账号管家调试端口恢复失败：{error}")
        return False
    return False

def connect_browser(playwright):
    try:
        resp = requests.get(f"{CDP_ENDPOINT}/json/version/", timeout=3)
        ws_url = resp.json()["webSocketDebuggerUrl"]
    except Exception:
        resp = requests.get(f"{CDP_ENDPOINT}/json/version", timeout=3)
        ws_url = resp.json()["webSocketDebuggerUrl"]
    return playwright.chromium.connect_over_cdp(ws_url, timeout=12000)


def find_internal_page(browser):
    for ctx in browser.contexts:
        for pg in ctx.pages:
            if INTERNAL_HOST_SUFFIX and INTERNAL_HOST_SUFFIX in pg.url:
                return pg
    return None


def is_internal_material_url(url):
    return "video-material-consumption" in str(url or "")


def can_use_internal_direct_target(target):
    """任一可连接的内部系统页面都应优先走页面级 CDP。

    页面级导出器会自行从内部系统首页导航到素材消耗页；不能因为当前
    hash 还停留在首页，就退回容易在 Onion 上卡住的浏览器级 Playwright。
    """
    if not isinstance(target, dict):
        return False
    return bool(
        target.get("webSocketDebuggerUrl")
        and INTERNAL_HOST_SUFFIX
        and INTERNAL_HOST_SUFFIX in str(target.get("url", "")).lower()
    )


LOGIN_PLATFORM_LABELS = ("目标登录平台", "example-login-platform")


def is_yuntian_login_platform_page(page):
    """识别账号管家的平台选择页，不依赖易变的 hash 路由。"""
    try:
        if "login#/login" in page.url:
            return True
    except Exception:
        pass
    try:
        return bool(
            page.evaluate(
                r"""
                (labels) => {
                    const visible = (el) => {
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden'
                            && s.display !== 'none';
                    };
                    const bodyText = (document.body?.innerText || '').replace(/\s+/g, ' ');
                    const hasHeading = bodyText.includes('选择登录平台');
                    const hasPlatform = Array.from(document.querySelectorAll('body *')).some((el) => {
                        if (!visible(el)) return false;
                        const text = (el.innerText || el.textContent || '').trim();
                        return text.length <= 40 && labels.some(
                            (label) => text === label || text.includes(label)
                        );
                    });
                    return hasHeading && hasPlatform;
                }
                """,
                list(LOGIN_PLATFORM_LABELS),
            )
        )
    except Exception:
        return False


def click_yuntian_login_platform(page, timeout=8):
    """在内部系统登录平台页选择云天平台。

    登录页的卡片文案在不同版本中出现过几种写法，
    卡片标题也不固定为 h2。优先使用 Playwright 文本定位，最后用 DOM 事件
    冒泡兜底，避免依赖具体的标签名或 CSS 类名。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for label in LOGIN_PLATFORM_LABELS:
            try:
                locator = page.get_by_text(label, exact=True)
                for index in range(locator.count()):
                    candidate = locator.nth(index)
                    if candidate.is_visible(timeout=250):
                        candidate.click(timeout=1200, force=True)
                        return True
            except Exception:
                pass

        try:
            clicked = page.evaluate(
                """
                (labels) => {
                    const visible = (el) => {
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden'
                            && s.display !== 'none';
                    };
                    const all = Array.from(document.querySelectorAll('body *'));
                    const target = all.find((el) => {
                        if (!visible(el)) return false;
                        const text = (el.innerText || el.textContent || '').trim();
                        if (!text || text.length > 40) return false;
                        return labels.some((label) => text === label || text.includes(label));
                    });
                    if (!target) return false;

                    let clickable = target;
                    for (let i = 0; i < 6 && clickable.parentElement; i += 1) {
                        const style = getComputedStyle(clickable);
                        const role = clickable.getAttribute('role') || '';
                        const tag = clickable.tagName.toLowerCase();
                        const rect = clickable.getBoundingClientRect();
                        if (['a', 'button'].includes(tag) || role === 'button'
                            || style.cursor === 'pointer'
                            || (clickable !== target && rect.width >= 180 && rect.height >= 180)) break;
                        clickable = clickable.parentElement;
                    }
                    clickable.scrollIntoView({block: 'center', inline: 'center'});
                    clickable.click();
                    return true;
                }
                """,
                list(LOGIN_PLATFORM_LABELS),
            )
            if clicked:
                return True
        except Exception:
            pass
        page.wait_for_timeout(300)
    return False

def load_sku_mapping():
    mapping = {}
    try:
        mapping_file = r"C:\Users\admin\AppData\Roaming\TRAE SOLO\ModularData\ai-agent\work-mode-projects\6a311aa29582b72d05e41929\素材库SKU映射表_待填写.xlsx"
        if os.path.exists(mapping_file):
            df = pd.read_excel(mapping_file)
            # 根据原始产品词建立映射
            for _, row in df.iterrows():
                product_word = row.get('原始产品词(第2段)')
                mapped_name = row.get('你填: 映射产品名')
                mapped_biz = row.get('你填: 映射业务线')
                
                if pd.notna(product_word) and pd.notna(mapped_name) and str(mapped_name).strip() != '未知':
                    mapping[str(product_word).strip()] = {
                        "产品名": str(mapped_name).strip(),
                        "业务线": str(mapped_biz).strip() if pd.notna(mapped_biz) else ""
                    }
    except Exception as e:
        print(f"读取映射表失败: {e}")
    return mapping

def get_sku_and_biz_tags(name, mapping):
    parts = name.split('-')
    tags = []
    found_sku = False
    
    # 尝试在所有部分中寻找映射表中存在的词
    for part in parts:
        word = part.strip()
        if word in mapping:
            mapped = mapping[word]
            tags.append(f"SKU:{mapped['产品名']}")
            if mapped['业务线'] and mapped['业务线'] != '未知':
                tags.append(mapped['业务线'])
            found_sku = True
            break  # 找到一个匹配的 SKU 就停止
            
    # 如果映射表里都没找到，并且名字至少有两段，默认取第2段（或第3段如果第1段是数字金额）作为 SKU 兜底
    if not found_sku and len(parts) > 1:
        # 有些名字第一段是消耗金额，第二段是日期，此时取第三段
        if '.' in parts[0] or parts[0].isdigit():
            if len(parts) > 2:
                fallback_word = parts[2].strip()
            else:
                fallback_word = parts[1].strip()
        else:
            fallback_word = parts[1].strip()
            
        tags.append(f"SKU:{fallback_word}")
        
    return tags

def get_consumption_level(val):
    if val >= 10000:
        return "消耗:10000以上"
    elif val >= 1000:
        return "消耗:1000-10000"
    elif val >= 100:
        return "消耗:100-1000"
    else:
        return "消耗:100以下"

def scan_eagle_library_via_api(target_library_id=None):
    index = {}
    try:
        url = 'http://localhost:41595/api/item/list'
        if target_library_id:
            url += f'?libraryId={target_library_id}'
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get('status') == 'success':
                items = data.get('data', [])
                for item in items:
                    item_url = item.get("url")
                    item_name = item.get("name", "")
                    item_id = item.get("id")
                    tags = item.get("tags", [])
                    
                    annotation = item.get("annotation", "")
                    exact_c = 0.0
                    if annotation.startswith("消耗:"):
                        try:
                            exact_c = float(annotation.replace("消耗:", "").strip())
                        except:
                            pass
                    
                    if exact_c == 0.0:
                        for t in tags:
                            if t.startswith("累计消耗:"):
                                try:
                                    exact_c = float(t.split(":")[1])
                                except:
                                    pass
                    
                    item_data = {
                        "id": item_id,
                        "tags": tags,
                        "name": item_name,
                        "url": item_url,
                        "exact_consumption": exact_c
                    }
                    
                    if item_url:
                        index[item_url] = item_data
                    if item_name:
                        index[item_name] = item_data
    except Exception as e:
        print(f"通过API扫描Eagle库失败: {e}")
    return index

def update_existing_item(item_data, new_consumption, sku_mapping, target_library_id=None):
    item_id = item_data.get("id")
    name = item_data.get("name", "")
    tags = item_data.get("tags", [])
    if not isinstance(tags, list):
        tags = []
        
    download_count = 1
    cumulative_consumption = item_data.get("exact_consumption", 0.0) + float(new_consumption)
    
    new_tags = []
    for tag in tags:
        if tag.startswith("累计下载次数:"):
            try:
                download_count = int(tag.split(":")[1])
            except:
                pass
        elif tag.startswith("累计消耗:") or tag.startswith("消耗:"):
            pass
        elif tag.startswith("SKU:") or tag in KNOWN_BUSINESS_LINES:
            pass
        else:
            new_tags.append(tag)
    
    download_count += 1
    
    new_tags.append(f"累计下载次数:{download_count}")
    new_tags.append(get_consumption_level(cumulative_consumption))
    
    sku_tags = get_sku_and_biz_tags(name, sku_mapping)
    for st in sku_tags:
        if st not in new_tags:
            new_tags.append(st)
    
    if item_id:
        try:
            update_payload = {
                "id": item_id,
                "tags": new_tags,
                "annotation": f"消耗: {cumulative_consumption:.2f}"
            }
            if target_library_id:
                update_payload["libraryId"] = target_library_id
            requests.post('http://localhost:41595/api/item/update', json=update_payload, timeout=3)
        except Exception as e:
            print(f"更新失败: {e}")
            return None, None
        
    return download_count, cumulative_consumption, sku_tags

def download_new_item(url, name, consumption, sku_mapping, target_library_id=None):
    level_tag = get_consumption_level(float(consumption))
    tags = ["累计下载次数:1", level_tag]
    
    sku_tags = get_sku_and_biz_tags(name, sku_mapping)
    for st in sku_tags:
        if st not in tags:
            tags.append(st)
    
    try:
        temp_dir = os.path.join(INTERNAL_REPORT_DIR, ".temp")
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir)
        
        # 获取文件扩展名
        ext = url.split('.')[-1].split('?')[0]
        if ext not in ['jpg', 'jpeg', 'png', 'gif', 'webp']:
            ext = 'jpg'
        
        temp_file = os.path.join(temp_dir, f"{name}.{ext}")
        
        # 下载图片
        img_response = requests.get(url, timeout=30)
        if img_response.status_code == 200:
            with open(temp_file, 'wb') as f:
                f.write(img_response.content)
            
            # 使用本地文件路径添加到 Eagle
            payload = {
                "items": [{
                    "path": temp_file,
                    "name": str(name),
                    "tags": tags,
                    "annotation": f"消耗: {float(consumption):.2f}"
                }]
            }
            
            if target_library_id:
                payload["libraryId"] = target_library_id
            
            res = requests.post('http://localhost:41595/api/item/addFromPaths', json=payload, timeout=30)
            
            # 删除临时文件
            try:
                os.remove(temp_file)
            except:
                pass
            
            if res.status_code == 200:
                result = res.json()
                if result.get('status') == 'success':
                    # 等待一小段时间让 Eagle 处理完成
                    time.sleep(0.1)
                    return True
        
    except Exception as e:
        print(f"添加失败 [{name}]: {e}")
        
    return False

def get_target_library_id():
    try:
        res = requests.get('http://localhost:41595/api/library/list', timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data.get('status') == 'success':
                libraries = data.get('data', [])
                for lib in libraries:
                    if lib.get('name') == '内部跑量素材库':
                        return lib.get('id')
    except Exception as e:
        print(f"获取库列表失败: {e}")
    return None


def build_consumption_filename(name, consumption, extension):
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', str(name)).strip()
    safe_name = safe_name or "未命名素材"
    try:
        consumption_text = f"{float(consumption):.2f}"
    except (TypeError, ValueError):
        consumption_text = "未知"
    return f"{consumption_text}-{safe_name}.{extension}"


def process_excel(excel_path):
    print(f"\n=================================")
    print(f"开始解析Excel并下载素材: {excel_path}")
    
    try:
        df = pd.read_excel(excel_path)
    except Exception as e:
        print(f"读取Excel失败: {e}")
        return
    
    if '预览地址' not in df.columns or '图片名称' not in df.columns or '消耗' not in df.columns:
        print("Excel中缺少必要的列（预览地址, 图片名称, 消耗）")
        return

    media_column = next(
        (column for column in ('媒体', '广告媒体', '投放媒体') if column in df.columns),
        None,
    )
    channel_column = next(
        (column for column in ('数据平台', '渠道', '流量渠道', '渠道平台') if column in df.columns),
        None,
    )
    if media_column is None:
        print("Excel未找到媒体列（媒体/广告媒体/投放媒体），旧媒体字段将留空")
    if channel_column is None:
        print("Excel未找到渠道列（数据平台/渠道/流量渠道/渠道平台），图片将进入未识别渠道目录")
    
    save_dir = INTERNAL_IMAGE_DIR
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)
    
    success_count = 0
    fail_count = 0
    skip_count = 0
    total = len(df)
    print(f"共发现 {total} 条数据需要处理...")
    print(f"保存目录: {save_dir}")
    media_mapping = {}
    capture_items = []
    
    for position, (_, row) in enumerate(df.iterrows(), start=1):
        url = row['预览地址']
        name = row['图片名称']
        consumption = row['消耗']
        
        if pd.isna(url) or pd.isna(name):
            skip_count += 1
            print(f"[{position}/{total}] 跳过无效数据")
            continue
            
        consumption = float(consumption) if pd.notna(consumption) else 0.0
        
        if consumption < 100:
            skip_count += 1
            print(f"[{position}/{total}] 跳过低消耗素材: {name}")
            continue
        
        try:
            ext = url.split('.')[-1].split('?')[0]
            if ext not in ['jpg', 'jpeg', 'png', 'gif', 'webp']:
                ext = 'jpg'
            
            filename = build_consumption_filename(name, consumption, ext)
            filepath = os.path.join(save_dir, filename)
            media_value = row.get(media_column, '') if media_column else ''
            media_text = str(media_value).strip() if pd.notna(media_value) else ''
            channel_value = row.get(channel_column, '') if channel_column else ''
            channel_text = str(channel_value).strip() if pd.notna(channel_value) else ''
            mapping_value = {"media": media_text, "channel": channel_text}
            
            if os.path.exists(filepath):
                media_mapping.setdefault(filename, mapping_value)
                capture_items.append({"file_name": filename, "source_url": str(url),
                    "source_content_id": str(name), "title": str(name),
                    "content_text": f"消费 {consumption:.2f}",
                    "raw": {"media": media_text, "channel": channel_text,
                            "data_platform": channel_text, "consumption": consumption}})
                print(f"[{position}/{total}] 跳过已存在: {filename}")
                skip_count += 1
                continue
            
            img_response = requests.get(url, timeout=30)
            if img_response.status_code == 200:
                with open(filepath, 'wb') as f:
                    f.write(img_response.content)
                media_mapping[filename] = mapping_value
                capture_items.append({"file_name": filename, "source_url": str(url),
                    "source_content_id": str(name), "title": str(name),
                    "content_text": f"消费 {consumption:.2f}",
                    "raw": {"media": media_text, "channel": channel_text,
                            "data_platform": channel_text, "consumption": consumption}})
                success_count += 1
                print(f"[{position}/{total}] ✓ 下载成功: {filename} (消耗:{consumption:.2f})")
            else:
                fail_count += 1
                print(f"[{position}/{total}] ✗ 下载失败: {name} (HTTP {img_response.status_code})")
        except Exception as e:
            fail_count += 1
            print(f"[{position}/{total}] ✗ 下载失败: {name} ({e})")

    mapping_path = os.path.join(INTERNAL_REPORT_DIR, "media-map.json")
    write_capture_items(OUTPUT_LAYOUT, capture_items)
    try:
        # 同一天的多次采集共用一个日期目录，映射表要合并而不是覆盖，
        # 否则后一次会把前一次（另一个方向）的媒体/渠道映射冲掉。
        merged_mapping = {}
        if os.path.isfile(mapping_path):
            try:
                with open(mapping_path, encoding="utf-8") as existing_file:
                    existing_mapping = json.load(existing_file)
                if isinstance(existing_mapping, dict):
                    merged_mapping.update(existing_mapping)
            except (OSError, ValueError):
                pass
        merged_mapping.update(media_mapping)
        with open(mapping_path, 'w', encoding='utf-8') as mapping_file:
            json.dump(merged_mapping, mapping_file, ensure_ascii=False, indent=2)
        print(f"媒体/渠道字段映射已保存（累计 {len(merged_mapping)} 项）: {mapping_path}")
    except Exception as e:
        print(f"保存媒体/渠道字段映射失败: {e}")
            
    print(f"处理完成！成功: {success_count}, 跳过: {skip_count}, 失败: {fail_count}")
    print(f"=================================\n")

def run_automation():
    download_dir = INTERNAL_REPORT_DIR
    if not os.path.exists(download_dir):
        os.makedirs(download_dir)
    print(f"批次目录: {INTERNAL_BATCH_DIR}")
    print(f"报表目录: {INTERNAL_REPORT_DIR}")
    print(f"原始素材目录: {INTERNAL_IMAGE_DIR}")

    if not check_and_start_browser():
        print("无法启动浏览器，请手动启动账号管家")
        return

    probe_only = os.environ.get("INTERNAL_EXPORT_PROBE") == "1"

    # 视频模式：API 直采（产品关键词 + 消耗 Top N），不走 Excel 导出
    if not probe_only and INTERNAL_MATERIAL_TYPE == "视频":
        print("内部系统视频模式：使用 API 直采（产品关键词筛选 + 消耗 Top N）...")
        try:
            from internal_export_cdp import run_direct_video_export
            keyword = INTERNAL_KEYWORD or "保险"
            if run_direct_video_export(
                INTERNAL_IMAGE_DIR,
                INTERNAL_REPORT_DIR,
                lambda items: write_capture_items(OUTPUT_LAYOUT, items),
                keyword=keyword,
                top_n=INTERNAL_TOP_N,
                filter_date=FILTER_DATE,
            ):
                return
            print("内部系统 API 直采视频失败。")
            raise SystemExit(1)
        except SystemExit:
            raise
        except Exception as error:
            print(f"API 直采视频模式不可用: {error}")
            raise SystemExit(1)

    direct_error = None
    direct_target_available = False
    try:
        from internal_export_cdp import find_internal_target, run_direct_export
        direct_target = find_internal_target()
        direct_target_available = can_use_internal_direct_target(direct_target)
        if direct_target_available:
            if is_internal_material_url(direct_target.get("url", "")):
                print("正在使用内部系统目标标签页直连模式（不会重启浏览器）...")
            else:
                print("内部系统标签页尚未进入素材消耗页，将通过页面级 CDP 自动导航...")
            if run_direct_export(
                download_dir,
                None if probe_only else process_excel,
                probe_only=probe_only,
                filter_date=FILTER_DATE,
            ):
                return
            print("内部系统目标标签页直连导出失败，请查看上方具体步骤。")
            raise SystemExit(1)
        print("9223 端口中没有可直连的内部系统标签页，尝试原 Playwright 模式...")
        direct_error = RuntimeError("未找到可直连的内部系统目标标签页")
    except SystemExit:
        raise
    except Exception as error:
        direct_error = error
        print(f"目标标签页直连模式不可用，尝试原 Playwright 模式: {error}")

    with sync_playwright() as p:
        try:
            browser = connect_browser(p)
        except Exception as e:
            print(f"Playwright 连接账号管家失败: {e}")
            if direct_error:
                print(f"此前目标标签页直连结果: {direct_error}")
            print("内部系统直连与 Playwright 两种模式均不可用")
            raise SystemExit(1)

        page = find_internal_page(browser)
        if page is None:
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()

        try:
            maybe_bring_to_front(page)
            print("1. 访问登录页面...")
            page.goto(INTERNAL_URL)
            page.wait_for_timeout(2000)

            if is_yuntian_login_platform_page(page):
                print("2. 点击“目标登录平台”登录平台...")
                try:
                    if not click_yuntian_login_platform(page):
                        raise RuntimeError("登录平台页未找到“目标登录平台”卡片")
                    page.wait_for_timeout(2000)

                    username = page.locator("input[type='text']")
                    password = page.locator("input[type='password']")
                    if username.count() > 0 and password.count() > 0:
                        print("3. 输入账号和密码...")
                        username.first.fill("后期-示例用户")
                        password.first.fill("liangkun2023")

                        print("4. 点击登录...")
                        page.locator("button[type='submit']").first.click()

                        print("等待登录成功并跳转...")
                        page.wait_for_timeout(5000)
                    else:
                        print("平台选择完成，当前会话无需再次输入账号密码")
                except Exception as e:
                    print("登录流程执行异常:", e)

                if is_yuntian_login_platform_page(page):
                    print("登录失败，请检查账号密码或是否有验证码拦截。")
                    page.wait_for_timeout(10000)
                    if is_yuntian_login_platform_page(page):
                        raise RuntimeError("内部系统仍停留在登录平台选择页")
            else:
                print("已登录，跳过登录步骤")

            if not is_internal_material_url(page.url):
                print("正在进入素材消耗统计明细页面...")
                page.goto(INTERNAL_URL, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                if is_yuntian_login_platform_page(page):
                    raise RuntimeError("选择目标登录平台后仍未进入内部系统")

            print("登录成功，当前URL:", page.url)

            if probe_only:
                if not is_internal_material_url(page.url):
                    raise RuntimeError("无标签恢复后未进入素材消耗统计明细页面")
                print("内部系统无标签恢复探测通过：已进入素材消耗统计明细页面")
                return

            print("查找菜单“创意中心”和“素材消耗统计明细”...")
            try:
                page.locator("text=创意中心").first.click(timeout=3000)
                page.wait_for_timeout(1000)
            except Exception as e:
                print("点击创意中心失败:", e)
                
            try:
                page.locator("text=素材消耗统计明细").first.click(timeout=3000)
                page.wait_for_timeout(3000)
            except Exception as e:
                print("点击素材消耗统计明细失败:", e)

            try:
                select_filter_date(page, FILTER_DATE)
            except Exception as e:
                if FILTER_DATE:
                    raise RuntimeError(f"无法复用原任务筛选日期 {FILTER_DATE}：{e}") from e
                print("选择昨天失败(可能是默认已经是昨天):", e)

            print("关闭日期选择浮层...")
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
                if page.locator(".el-picker-panel.el-date-range-picker").count() > 0:
                    page.mouse.click(10, 300)
                    page.wait_for_timeout(500)
                page.locator(".el-picker-panel.el-date-range-picker").first.wait_for(state="hidden", timeout=3000)
            except Exception as e:
                print("关闭日期浮层时跳过:", e)

            print("选择“图片素材类型”...")
            try:
                page.locator("input[placeholder='选择素材类型']").click(timeout=3000)
                page.wait_for_timeout(1000)
                page.locator("li.el-select-dropdown__item", has_text="图片").filter(has_text="图片").first.click(timeout=3000)
                page.wait_for_timeout(1000)
            except Exception as e:
                print("选择图片素材类型失败:", e)

            print("清理“产品”残留筛选...")
            clear_product_filter(page)
            page.wait_for_timeout(500)

            print("点击查询...")
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
                page.locator("button", has_text="查询").first.click(timeout=3000)
                page.wait_for_timeout(5000)
            except Exception as e:
                print("点击查询失败:", e)

            total = read_result_total(page)
            if total == 0:
                raise RuntimeError(
                    "内部系统查询结果为 0 条，已停止导出："
                    "请检查账号管家页面上的时间范围、产品、素材类型筛选是否有残留条件"
                )
            if total is not None:
                print(f"✓ 查询结果共 {total} 条")

            # 导出前核实查询真正生效的日期。长期存活的页面会把「显示值」和
            # 「内部模型」分开存，只写 input.value 时查询仍用旧日期 ——
            # 2026-09-22 实测采错整天数据（输入框 09-21、表格整列 09-20）。
            try:
                import datetime as _dt
                from internal_export_cdp import read_applied_date

                expected_date = FILTER_DATE or (
                    _dt.date.today() - _dt.timedelta(days=1)
                ).isoformat()
                applied_date = read_applied_date(page)
                if applied_date and applied_date != expected_date:
                    raise RuntimeError(
                        f"查询日期未生效：期望 {expected_date}，表格实际为 {applied_date}。"
                        "已中止导出，避免采错整天的数据。"
                    )
                if applied_date:
                    print(f"✓ 已核实查询日期生效：{applied_date}")
            except RuntimeError:
                raise
            except Exception as exc:
                print(f"  ⚠ 日期核实跳过：{exc}")

            print("正在全选数据...")
            try:
                select_all_checkbox = page.locator(".el-table__header-wrapper .el-checkbox, thead .el-checkbox").first
                if select_all_checkbox.count() > 0:
                    select_all_checkbox.click(timeout=3000)
                else:
                    page.locator(".el-checkbox").first.click(timeout=3000)
                page.wait_for_timeout(2000)
                print("全选操作完成")
            except Exception as e:
                print("全选失败:", e)

            print("点击导出按钮...")
            try:
                export_btn = page.locator("button", has_text="导出数据").first
                with page.expect_download(timeout=30000) as download_info:
                    export_btn.click()
                
                download = download_info.value
                file_path = os.path.join(download_dir, download.suggested_filename)
                download.save_as(file_path)
                print(f"数据已成功导出并保存至: {file_path}")
                
                process_excel(file_path)
                
            except Exception as e:
                print(f"导出失败: {e}")

        except Exception as e:
            print(f"自动化执行过程中发生错误: {e}")
            raise
        finally:
            try:
                page.wait_for_timeout(3000)
            except:
                pass

if __name__ == "__main__":
    try:
        run_automation()
    except Exception as error:
        print(f"CAPTURE_ERROR|{type(error).__name__}|{error}", flush=True)
        raise
