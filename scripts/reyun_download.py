#!/usr/bin/env python
# -*- coding: utf-8 -*-
import sys
import io
import os

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
import time
import re
import requests
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from output_layout import get_batch_layout, write_capture_items
from account_manager import open_platform_application
from task_browser import task_target_scope

OUTPUT_LAYOUT = get_batch_layout("热云")
SAVE_DIR = str(OUTPUT_LAYOUT["raw_dir"])
CDP_ENDPOINT = os.environ.get("MATERIAL_DOWNLOADER_CDP_ENDPOINT", "http://127.0.0.1:9223").rstrip("/")
SITE_URL = "https://data.insightrackr.com/"
PLATFORM_NAME = "热云"
BACKGROUND_MODE = os.environ.get("MATERIAL_DOWNLOADER_BACKGROUND_MODE", "true").strip().lower() not in {"0", "false", "no", "off"}
LOGIN_WAIT_SECONDS = max(30, int(os.environ.get("REYUN_LOGIN_WAIT_SECONDS", "120")))
ACCOUNT_HINT = os.environ.get("REYUN_ACCOUNT_HINT", "设计部").strip()


def maybe_bring_to_front(page):
    if not BACKGROUND_MODE:
        page.bring_to_front()


def connect_browser(playwright):
    # 先确保 9223 就绪：不通会自动拉起账号管家并等待
    try:
        from cdp_guard import ensure_cdp, get_cdp_endpoint
        if not ensure_cdp():
            raise RuntimeError("9223 调试端口不可用，请开启账号管家调试端口后重试")
        global CDP_ENDPOINT
        CDP_ENDPOINT = get_cdp_endpoint()
    except ImportError:
        pass
    try:
        resp = requests.get(f"{CDP_ENDPOINT}/json/version/", timeout=3)
        ws_url = resp.json()["webSocketDebuggerUrl"]
    except Exception:
        resp = requests.get(f"{CDP_ENDPOINT}/json/version", timeout=3)
        ws_url = resp.json()["webSocketDebuggerUrl"]
    target = open_platform_application(
        CDP_ENDPOINT,
        "热云",
        ACCOUNT_HINT,
        ("insightrackr",),
        timeout=30,
    )
    if target:
        print(f"[账号管家] 已通过“{ACCOUNT_HINT}”账户打开热云")
    else:
        print("[账号管家] 未能打开热云应用，将检查已有登录会话")
    return playwright.chromium.connect_over_cdp(ws_url)


def find_page(browser):
    """找到最新的素材搜索页(search/material),搜索会打开新标签页"""
    pages = []
    for ctx in browser.contexts:
        for pg in ctx.pages:
            if "insightrackr" in pg.url:
                pages.append(pg)
    return pages[-1] if pages else None


def find_creative_page(browser, initial_pages=()):
    """找到创意洞察页(app/detail/.../creative),优先返回此页面"""
    initial_ids = {id(page) for page in initial_pages}
    creative_pages = []
    all_pages = []
    for ctx in browser.contexts:
        for pg in ctx.pages:
            if id(pg) in initial_ids:
                continue
            if "insightrackr" in pg.url:
                all_pages.append(pg)
                if "app/detail" in pg.url and "creative" in pg.url:
                    creative_pages.append(pg)
    if creative_pages:
        return creative_pages[-1]
    return all_pages[-1] if all_pages else None


def _visible(element):
    try:
        return bool(element and element.is_visible())
    except Exception:
        try:
            return bool(element.evaluate(
                "el => Boolean(el && el.getBoundingClientRect().width && el.getBoundingClientRect().height)"
            ))
        except Exception:
            return False


def is_reyun_login_page(page):
    try:
        parsed = urlparse(str(page.url))
        if parsed.hostname == "data.insightrackr.com" and parsed.path.startswith("/login"):
            return True
        return bool(page.query_selector("input[type='password']"))
    except Exception:
        return False


def is_reyun_application_page(page):
    try:
        parsed = urlparse(str(page.url))
        if parsed.hostname != "data.insightrackr.com" or parsed.path.startswith("/login"):
            return False
        return not bool(page.query_selector("input[type='password']"))
    except Exception:
        return False


def accept_reyun_cookie_notice(page):
    for label in ("全部接受", "接受全部"):
        try:
            candidates = page.get_by_text(label, exact=True)
            for index in range(min(candidates.count(), 5)):
                button = candidates.nth(index)
                if _visible(button):
                    button.click(timeout=2000)
                    print("LOGIN_STATUS|COOKIE_ACCEPTED|热云|已接受 Cookie 提示")
                    return True
        except Exception:
            continue
    return False


def reyun_login_form_state(page):
    try:
        return page.evaluate("""
        () => {
            const password = document.querySelector("input[type='password']");
            const inputs = Array.from(document.querySelectorAll('input'));
            const account = inputs.find((input) => input !== password && input.type !== 'checkbox');
            return {
                accountReady: Boolean(account?.value),
                passwordReady: Boolean(password?.value),
            };
        }
        """)
    except Exception:
        return {"accountReady": False, "passwordReady": False}


def ensure_reyun_login_agreement(page):
    checkbox = page.query_selector(".el-checkbox")
    if not checkbox:
        return False
    checked = checkbox.evaluate("""
    el => Boolean(
        el.classList.contains('is-checked')
        || el.querySelector("input[type='checkbox']")?.checked
    )
    """)
    if checked:
        return True
    checkbox.click()
    time.sleep(0.2)
    return bool(checkbox.evaluate("""
    el => Boolean(
        el.classList.contains('is-checked')
        || el.querySelector("input[type='checkbox']")?.checked
    )
    """))


def ensure_reyun_logged_in(page, timeout=LOGIN_WAIT_SECONDS):
    if is_reyun_application_page(page):
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        time.sleep(0.5)
        if is_reyun_application_page(page):
            return True

    print(f"LOGIN_STATUS|CHECKING|热云|最多等待 {int(timeout)} 秒")
    deadline = time.time() + max(1, timeout)
    submitted = False
    while time.time() < deadline:
        if is_reyun_application_page(page):
            time.sleep(0.5)
            if is_reyun_application_page(page):
                print(f"LOGIN_STATUS|READY|热云|{page.url}")
                return True
        if is_reyun_login_page(page):
            accept_reyun_cookie_notice(page)
            state = reyun_login_form_state(page)
            if state.get("accountReady") and state.get("passwordReady"):
                if ensure_reyun_login_agreement(page) and not submitted:
                    submit = page.query_selector("button.submit")
                    if submit and _visible(submit):
                        submit.click()
                        submitted = True
                        print("LOGIN_STATUS|SUBMITTED|热云|已使用账号管家预填账户提交登录")
        time.sleep(1)

    raise RuntimeError(f"热云登录恢复超时（页面={page.url}），请检查账号管家中的设计部账户")


def apply_keyword_search(page, keyword):
    """主搜索框搜索APP名,会弹出下拉面板"""
    if not keyword:
        return page
    kw_input = page.query_selector('input[placeholder*="搜索"]')
    if not kw_input:
        print("未找到主搜索框")
        return page
    kw_input.evaluate("el => el.click()")
    time.sleep(0.3)
    kw_input.fill(keyword)
    print(f"✓ 填入APP名:{keyword}")
    time.sleep(0.3)
    kw_input.press("Enter")
    print("✓ 已回车触发搜索")
    time.sleep(3)
    return page


def click_first_product_in_dropdown(page, keyword):
    """在搜索下拉面板的「单独产品」区块中,点击第一个产品
    面板结构: 单独产品区块 → 产品列表 → 第一个产品
    """
    try:
        clicked = page.evaluate(r"""(kw) => {
            const productDivs = [...document.querySelectorAll('.flex.flex-col.gap-2')];
            for (const div of productDivs) {
                const titleDiv = div.querySelector('.font-bold');
                if (titleDiv && titleDiv.innerText.trim() === '单独产品') {
                    const products = [...div.querySelectorAll('[class*="product"], .flex, div')];
                    for (const p of products) {
                        const text = (p.innerText || '').trim();
                        if (text.includes(kw) && p.offsetParent !== null && p !== titleDiv.parentElement) {
                            p.click();
                            return true;
                        }
                    }
                }
            }
            return false;
        }""", keyword)
        
        if clicked:
            print(f"✓ 已点击「单独产品」中的第一个 {keyword}")
            time.sleep(4)
            return True
        else:
            print(f"⚠ 未在下拉面板中找到 {keyword}")
            return False
    except Exception as e:
        print(f"⚠ 点击产品失败: {e}")
        return False


def click_creative_insight_tab(page):
    """点击「创意洞察」标签"""
    tabs = page.query_selector_all('.el-tabs__item, [role="tab"], a, span')
    for t in tabs:
        text = (t.inner_text() or "").strip()
        if text == "创意洞察":
            t.evaluate("el => el.click()")
            print("✓ 切换到「创意洞察」")
            time.sleep(3)
            return True
    print("⚠ 未找到「创意洞察」标签")
    return False


def select_filter_by_label(page, filter_label, option_text):
    """按下拉框标签名(如"流量渠道")定位并选择选项。
    热云的流量渠道等筛选是复选框面板,需要:
    1. 点击下拉框打开面板
    2. 勾选目标复选框(如"腾讯广告")
    3. 点击面板内的"确定"按钮
    
    特殊情况:如果下拉框当前显示的文本已经是目标值,跳过选择
    """
    if not option_text:
        return
    
    result = page.evaluate(r"""(args) => {
        const [label, targetText] = args;
        const sels = [...document.querySelectorAll('.el-select')];
        for (let i = 0; i < sels.length; i++) {
            const ph = sels[i].querySelector('.el-select__placeholder');
            const phText = ph ? ph.innerText.trim() : '';
            const input = sels[i].querySelector('input');
            const inputPh = input ? input.placeholder.trim() : '';
            const txt = (sels[i].innerText || '').trim().split('\n')[0];

            // 已经选中目标值时，下拉框不再显示原始占位标签。
            if (txt === targetText || phText === targetText) {
                return {idx: i, alreadySelected: true};
            }
            
            if (phText === label || inputPh === label || txt === label) {
                return {idx: i, alreadySelected: false};
            }
        }
        return {idx: -1, alreadySelected: false};
    }""", [filter_label, option_text])
    
    if result['idx'] < 0:
        print(f"⚠ 未找到下拉框:{filter_label}")
        return
    
    if result['alreadySelected']:
        print(f"  ✓ 「{filter_label}」已选中:{option_text}")
        return
    
    page.evaluate(r"""(i) => {
        const sels = [...document.querySelectorAll('.el-select')];
        sels[i].scrollIntoView();
        const trigger = sels[i].querySelector('.el-select__wrapper') || sels[i];
        trigger.click();
    }""", result['idx'])
    time.sleep(2)
    
    checkbox_found = page.evaluate(r"""(text) => {
        const checkboxes = [...document.querySelectorAll('.el-checkbox')];
        for (const cb of checkboxes) {
            const label = (cb.innerText || '').trim();
            if (label === text && cb.offsetParent !== null) {
                cb.click();
                return true;
            }
        }
        return false;
    }""", option_text)
    
    if checkbox_found:
        print(f"  ✓ 已勾选「{filter_label}」→ {option_text}")
        time.sleep(1)
        
        confirm_clicked = page.evaluate(r"""() => {
            const buttons = [...document.querySelectorAll('button')];
            for (const b of buttons) {
                const t = (b.innerText || '').trim();
                if (t === '确定' && b.offsetParent !== null && b.className.includes('el-button--primary')) {
                    b.click();
                    return true;
                }
            }
            return false;
        }""")
        
        if confirm_clicked:
            print(f"  ✓ 已点击面板内「确定」按钮")
            time.sleep(2)
        else:
            print(f"  ⚠ 未找到面板内「确定」按钮")
    else:
        option_clicked = page.evaluate(r"""(text) => {
            const options = [
                ...document.querySelectorAll('.el-select-dropdown__item, [role="option"]')
            ];
            for (const option of options) {
                if (option.offsetParent !== null &&
                    (option.innerText || '').trim() === text &&
                    !option.classList.contains('is-disabled')) {
                    option.click();
                    return true;
                }
            }
            return false;
        }""", option_text)

        if option_clicked:
            print(f"  ✓ 已选择「{filter_label}」→ {option_text}")
            time.sleep(2)
        else:
            print(f"  ⚠ 「{filter_label}」未找到选项:{option_text}")
    
    page.keyboard.press("Escape")
    time.sleep(0.5)


def click_confirm_button(page):
    """点击筛选区右下角的蓝色"确定"按钮,应用筛选条件"""
    try:
        clicked = page.evaluate(r"""() => {
            const buttons = [...document.querySelectorAll('button')];
            for (const b of buttons) {
                const t = (b.innerText || '').trim();
                if ((t === '确定' || t === '确认') && 
                    b.offsetParent !== null && 
                    !b.disabled &&
                    !b.className.includes('is-disabled')) {
                    const rect = b.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0 && rect.y > 0) {
                        b.scrollIntoView();
                        b.click();
                        return true;
                    }
                }
            }
            return false;
        }""")
        
        if clicked:
            print("✓ 已点击「确定」按钮,等待页面加载...")
            time.sleep(3)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            time.sleep(2)
            return True
        else:
            print("⚠ 未找到「确定」按钮")
            return False
    except Exception as e:
        print(f"⚠ 点击确定按钮失败: {e}")
        return False


def click_sort_button(page, sort_text):
    """点击排序按钮,如"曝光预估"、"首次发现时间"
    热云的排序是在素材列表加载后才显示的横排按钮
    """
    if not sort_text:
        return
    
    clicked = page.evaluate(r"""(text) => {
        const all = [...document.querySelectorAll('*')];
        for (const el of all) {
            const t = (el.innerText || '').trim();
            if (t === text && el.offsetParent !== null) {
                const isClickable = el.tagName === 'BUTTON' || 
                                   el.onclick !== null || 
                                   el.className.includes('btn') ||
                                   el.className.includes('sort') ||
                                   el.style.cursor === 'pointer';
                if (isClickable || el.tagName === 'DIV' || el.tagName === 'SPAN') {
                    el.scrollIntoView();
                    el.click();
                    return true;
                }
            }
        }
        return false;
    }""", sort_text)
    
    if clicked:
        print(f"✓ 排序:{sort_text}")
        time.sleep(2)
    else:
        print(f"⚠ 未找到排序按钮:{sort_text}")


def download_image(url, filepath, session):
    try:
        headers = {"Referer": SITE_URL}
        resp = session.get(url, timeout=15, headers=headers)
        resp.raise_for_status()
        with open(filepath, "wb") as f:
            f.write(resp.content)
        return True
    except Exception as e:
        print(f"下载失败 {url}: {e}")
        return False


def find_scroll_container(page):
    for sel in ['.el-scrollbar__wrap', 'main', '.material-list', '[class*="list"]']:
        el = page.query_selector(sel)
        if el:
            return el
    return None


def scroll_to_bottom(scroll_container, max_rounds=15):
    prev_h = 0
    stable = 0
    for _ in range(max_rounds):
        scroll_container.evaluate("el => el.scrollBy(0, el.clientHeight)")
        time.sleep(1.2)
        sh = scroll_container.evaluate("el => el.scrollHeight")
        st = scroll_container.evaluate("el => el.scrollTop")
        ch = scroll_container.evaluate("el => el.clientHeight")
        if st + ch >= sh - 50:
            if sh == prev_h:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            prev_h = sh
        else:
            stable = 0


def collect_page_materials(page):
    """采集本页素材图 URL(adinsights-new-hk.oss域名,过滤logo/icon等噪声)"""
    return page.evaluate(r"""() => {
        const urls = [];
        const seen = new Set();
        document.querySelectorAll('img').forEach(img => {
            const s = img.src || '';
            if (!s.startsWith('http')) return;
            if (!s.includes('adinsights')) return;
            if (/logo|icon|avatar|default/i.test(s)) return;
            const w = img.naturalWidth || img.width || 0;
            const h = img.naturalHeight || img.height || 0;
            if (w < 200 || h < 200) return;
            if (!seen.has(s)) { seen.add(s); urls.push(s); }
        });
        document.querySelectorAll('video[poster]').forEach(v => {
            const s = v.poster || '';
            if (s.startsWith('http') && s.includes('adinsights') && !seen.has(s)) {
                seen.add(s); urls.push(s);
            }
        });
        return urls;
    }""")


def goto_next_page(page, scroll_container):
    next_btn = page.query_selector(".el-pagination button.btn-next") or \
        page.query_selector('.btn-next') or \
        page.query_selector('[class*="next"]')
    if not next_btn:
        return False
    disabled = next_btn.get_attribute("disabled")
    if disabled is not None:
        return False
    cls = next_btn.get_attribute("class") or ""
    if "is-disabled" in cls or "disabled" in cls:
        return False
    next_btn.evaluate("el => el.click()")
    time.sleep(2)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    if scroll_container:
        scroll_container.evaluate("el => el.scrollTo(0, 0)")
    time.sleep(1)
    return True


def main(keyword="", traffic="", creative_type="", sort="", max_downloads=500):
    today = OUTPUT_LAYOUT["batch_date"]
    base_dir = Path(SAVE_DIR)
    base_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.trust_env = False  # 忽略系统代理设置，避免代理软件未启动时采集下载全部失败
    try:
        from cdp_guard import ensure_cdp, get_cdp_endpoint
        if not ensure_cdp():
            raise RuntimeError("9223 调试端口不可用，请开启账号管家后重试")
        global CDP_ENDPOINT
        CDP_ENDPOINT = get_cdp_endpoint()
    except ImportError:
        pass

    with task_target_scope(
        CDP_ENDPOINT,
        "热云",
        url_keywords=("insightrackr",),
    ), sync_playwright() as p:
        browser = connect_browser(p)
        bootstrap_page = find_page(browser)
        if not bootstrap_page:
            print("未找到已登录的热云页面(账号管家 9223端口)")
            return

        ensure_reyun_logged_in(bootstrap_page)

        context = next(
            ctx for ctx in browser.contexts if bootstrap_page in ctx.pages
        )
        task_initial_pages = tuple(
            existing_page
            for ctx in browser.contexts
            for existing_page in ctx.pages
        )
        page = context.new_page()
        page.goto(SITE_URL, wait_until="domcontentloaded", timeout=60000)
        ensure_reyun_logged_in(page)
        maybe_bring_to_front(page)
        time.sleep(1)

        print(f"搜索APP:{keyword}, 筛选:创意类型={creative_type}, 排序={sort}")

        apply_keyword_search(page, keyword)
        
        if not click_first_product_in_dropdown(page, keyword):
            print("未在搜索下拉面板中找到产品")
            return
        
        page = find_creative_page(browser, task_initial_pages)
        if not page:
            print("未找到创意洞察页")
            return
        
        maybe_bring_to_front(page)
        time.sleep(2)
        
        if "creative" not in page.url:
            if not click_creative_insight_tab(page):
                print("产品详情页未找到「创意洞察」标签")
                return
            time.sleep(2)
            page = find_creative_page(browser, task_initial_pages)
            if not page:
                print("点击创意洞察后未找到对应页面")
                return
            maybe_bring_to_front(page)
            time.sleep(1)
        
        select_filter_by_label(page, "创意类型", creative_type)
        
        click_confirm_button(page)
        
        time.sleep(2)
        
        click_sort_button(page, sort)

        time.sleep(2)
        print("筛选已应用,等待结果加载")

        scroll_container = find_scroll_container(page)
        if not scroll_container:
            print("未找到滚动容器")
            return

        print(f"开始采集素材（上限 {max_downloads} 张，逐页翻页）...")
        collected = []
        seen_keys = set()
        page_no = 1

        while len(collected) < max_downloads:
            scroll_to_bottom(scroll_container)
            page_urls = collect_page_materials(page)
            new_cnt = 0
            for url in page_urls:
                key = url.split("?")[0]
                if key not in seen_keys:
                    seen_keys.add(key)
                    collected.append(url)
                    new_cnt += 1
            print(f"第 {page_no} 页：本页 {len(page_urls)} 个，新增 {new_cnt} 个，累计 {len(collected)} 个")

            if len(collected) >= max_downloads:
                break
            if not goto_next_page(page, scroll_container):
                print("已到最后一页，无法继续翻页")
                break
            page_no += 1

        if len(collected) > max_downloads:
            collected = collected[:max_downloads]
        print(f"共采集 {len(collected)} 个素材，开始下载...")

        capture_items = []
        for idx, url in enumerate(collected, 1):
            clean = re.sub(r'[^\w\-.]', '_', url.split('/')[-1].split('?')[0])
            filename = f"[{PLATFORM_NAME}]_{today}_{idx:04d}_{clean[:60]}.jpg"
            filepath = base_dir / filename
            print(f"下载 {idx}/{len(collected)}: {url[:80]}...")
            if download_image(url, filepath, session):
                capture_items.append({
                    "file_name": filename,
                    "source_url": url,
                    "source_content_id": url.split("?")[0].rsplit("/", 1)[-1],
                    "title": keyword or clean[:60],
                    "content_text": keyword,
                    "raw": {"creative_type": creative_type, "sort": sort},
                })
                print(f"✓ 已保存：{filename}")
            time.sleep(0.3)

        write_capture_items(OUTPUT_LAYOUT, capture_items)
        print(f"\n下载完成，保存至：{base_dir}")
        print("下载任务结束（未执行去重或分类；如需整理请使用“整理工坊”）")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="热云 insightrackr 素材下载器")
    parser.add_argument("--keyword", default="", help="搜索APP名,如 快手")
    parser.add_argument("--type", dest="creative_type", default="", help="创意类型，如 图片；留空表示全部")
    parser.add_argument("--sort", default="曝光预估", help="排序方式，如 曝光预估")
    parser.add_argument("--max", type=int, default=500, help="下载数量上限")
    args = parser.parse_args()
    main(args.keyword, "", args.creative_type, args.sort, args.max)
