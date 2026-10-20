#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import time
import os
import sys
import io
import requests
import re
import hashlib
import argparse
import subprocess
from account_manager import open_platform_application
from output_layout import get_batch_layout, write_capture_items
from task_browser import close_new_targets, snapshot_target_ids
from direct_cdp import CDPConnection, DirectBrowser, DirectPage, connect_target_page

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

OUTPUT_LAYOUT = get_batch_layout("妙思")
SAVE_DIR = str(OUTPUT_LAYOUT["raw_dir"])
MAX_DOWNLOADS = 5000
SCORE_THRESHOLD = 40
CAPTURE_ITEMS = []

# 滚动采集的收尾判定。
# 妙思列表是懒加载的，页面慢的时候连续几轮读不到新卡片并不代表已经到底，
# 所以不能只看"空轮次数"就收工：必须同时确认滚动容器真的到了底部。
NO_NEW_ROUND_LIMIT = max(2, int(os.environ.get("ADMUSE_NO_NEW_ROUND_LIMIT", "8")))
# 容器高度连续多少轮不再增长，才认为加载真的停下来了。
# 注意：贴底时 scrollBy 是空操作、不会产生 scroll 事件，页面因此可能永远不会被
# 再次触发加载。所以高度不增长时要"回拉一屏再到底"轻推一次，而不是直接收工。
NO_GROWTH_ROUND_LIMIT = max(3, int(os.environ.get("ADMUSE_NO_GROWTH_ROUND_LIMIT", "10")))
NO_NEW_ROUND_HARD_LIMIT = NO_NEW_ROUND_LIMIT * 4   # 兜底：读不到滚动状态时也不会无限滚
SCROLL_WAIT_SECONDS = max(1.0, float(os.environ.get("ADMUSE_SCROLL_WAIT_SECONDS", "5")))
SCROLL_BOTTOM_TOLERANCE_PX = 12
MAX_SCROLL_ROUNDS = max(10, int(os.environ.get("ADMUSE_MAX_SCROLL_ROUNDS", "300")))
# 每次滚动前进多少屏。妙思是虚拟滚动列表，DOM 里只保留可视窗口附近的卡片，
# 一次跳两屏会把中间那一屏整段跳过（从未渲染 → 永远采集不到），
# 所以默认一次只前进一屏，宁可多滚几轮也不漏素材。
SCROLL_STEP_RATIO = max(0.5, float(os.environ.get("ADMUSE_SCROLL_STEP_RATIO", "1")))

CDP_ENDPOINT = os.environ.get("MATERIAL_DOWNLOADER_CDP_ENDPOINT", "http://127.0.0.1:9223").rstrip("/")
ADMUSE_IDEA_URL = "https://admuse.qq.com/#/idea"
BACKGROUND_MODE = os.environ.get("MATERIAL_DOWNLOADER_BACKGROUND_MODE", "true").strip().lower() not in {"0", "false", "no", "off"}
LOGIN_WAIT_SECONDS = max(15, int(os.environ.get("ADMUSE_LOGIN_WAIT_SECONDS", "90")))
ADMUSE_QQ_USERNAME_ENV = "ADMUSE_QQ_USERNAME"
ADMUSE_QQ_PASSWORD_ENV = "ADMUSE_QQ_PASSWORD"
ADMUSE_QQ_CREDENTIALS_FILE_ENV = "ADMUSE_QQ_CREDENTIALS_FILE"
ADMUSE_QQ_LOGIN_LABELS = ("QQ 登录", "QQ登录", "QQ 登陆", "使用QQ登录")
ADMUSE_LOGIN_OPEN_LABELS = ("登录/注册", "立即登录", "请先登录", "登录查看更多")
ADMUSE_ACCOUNT_MANAGER_PLATFORM = os.environ.get(
    "ADMUSE_ACCOUNT_MANAGER_PLATFORM", "妙思"
).strip()
ADMUSE_ACCOUNT_HINT = os.environ.get("ADMUSE_ACCOUNT_HINT", "设计部").strip()

APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
ADMUSE_QQ_CREDENTIALS_RELATIVE = os.path.join(
    "data", "shared", "material_collections", "妙思", "账号密码.txt"
)
# 独立版可以把自己的凭据放这里，优先级最高、最不容易搞错
ADMUSE_QQ_CREDENTIALS_LOCAL = os.path.join(APP_ROOT, "userdata", "admuse_qq_credentials.txt")
# 常见的兄弟工程名（独立版把 data/shared 放在这些工程里，而不是本工程内）
ADMUSE_QQ_SIBLING_WORKSPACES = ("sibling_workspace_a", "sibling_workspace_b")


def admuse_qq_credentials_candidates():
    """按优先级列出可能的凭据文件位置。

    **不能只认固定的「往上三级」**：上游原版把 workspace 放在 `resources/` 下，
    而独立版的 `data/shared` 在**兄弟工程**里（sibling_workspace_a /
    sibling_workspace_b）。路径写死会让凭据永远读不到 —— 表现为
    「账号掉线后不会自动补登录」，而且失败还是静默的。
    """
    candidates: list[str] = []

    def add(path):
        resolved = os.path.abspath(path)
        if resolved not in candidates:
            candidates.append(resolved)

    # 1) 应用自带的覆盖文件（丢一份进去就能用，最直观）
    add(ADMUSE_QQ_CREDENTIALS_LOCAL)
    # 2) 从本文件往上逐级找 data/shared
    base = os.path.dirname(os.path.abspath(__file__))
    for _ in range(4):
        add(os.path.join(base, ADMUSE_QQ_CREDENTIALS_RELATIVE))
        base = os.path.dirname(base)
    # 3) 兄弟工程里的 data/shared
    parent = os.path.dirname(APP_ROOT)
    for name in ADMUSE_QQ_SIBLING_WORKSPACES:
        add(os.path.join(parent, name, ADMUSE_QQ_CREDENTIALS_RELATIVE))
    return candidates


def resolve_admuse_qq_credentials_file():
    """返回第一个真实存在的凭据文件；都没有则返回 None。"""
    for path in admuse_qq_credentials_candidates():
        if os.path.isfile(path):
            return path
    return None


DEFAULT_ADMUSE_QQ_CREDENTIALS_FILE = (
    resolve_admuse_qq_credentials_file()
    or os.path.join(WORKSPACE_ROOT, ADMUSE_QQ_CREDENTIALS_RELATIVE)
)


def maybe_bring_to_front(page):
    if not BACKGROUND_MODE:
        page.bring_to_front()


def focus_auth_page(page):
    try:
        page.bring_to_front()
    except Exception:
        pass


def check_and_start_browser():
    # 统一走 cdp_guard：通就直接用；不通自动定位 onion.exe（运行中进程/常见路径/PATH）启动并等待就绪
    try:
        from cdp_guard import ensure_cdp, get_cdp_endpoint
        # 采集任务必须能自愈：若账号管家是手动启动且没有 9223，
        # cdp_guard 会在等待后按带登录态的用户目录重启并开启调试端口。
        if ensure_cdp():
            global CDP_ENDPOINT
            CDP_ENDPOINT = get_cdp_endpoint()
            print("✓ 账号管家调试端口 9223 已就绪")
            return True
        print("✗ 9223 调试端口不可用，请手动开启后重试")
        return False
    except ImportError:
        pass
    try:
        resp = requests.get(f"{CDP_ENDPOINT}/json/version", timeout=3)
        if resp.status_code == 200:
            print("✓ 账号管家调试端口 9223 已开启")
            return True
    except Exception:
        pass

    print("✗ 账号管家调试端口 9223 未开启，正在启动...")

    onion_path = r"C:\Program Files\Oray\OrayOnion\onion.exe"

    if not os.path.exists(onion_path):
        print(f"错误: 账号管家未找到，路径: {onion_path}")
        return False

    try:
        subprocess.Popen([onion_path, "--remote-debugging-port=9223"], shell=False)

        print("等待账号管家启动...")
        for i in range(15):
            time.sleep(1)
            try:
                resp = requests.get(f"{CDP_ENDPOINT}/json/version", timeout=2)
                if resp.status_code == 200:
                    print(f"✓ 账号管家已启动 (等待 {i+1} 秒)")
                    time.sleep(2)
                    return True
            except Exception:
                pass

        print("警告: 账号管家启动超时，但会继续尝试连接")
        return True

    except Exception as e:
        print(f"启动账号管家失败: {e}")
        return False

def connect_browser(playwright):
    """Connect to one 妙思 page instead of enumerating the whole browser.

    Oray Onion can answer ``/json/version`` while its browser-level CDP
    endpoint stalls during Playwright's context bootstrap.  A page-level
    socket avoids that stall and the DirectContext adapter still provides the
    handful of Playwright APIs used below.
    """
    try:
        direct = connect_target_page(
            CDP_ENDPOINT,
            "admuse.qq.com",
            timeout=10.0,
            open_url=ADMUSE_IDEA_URL,
        )
        print("[CDP] 已使用妙思页面级连接（避免浏览器级枚举卡住）")
        return direct
    except Exception as direct_error:
        print(f"[CDP] 页面级连接失败：{direct_error}；尝试 Playwright 兼容连接（最多 15 秒）")

    # Keep a bounded fallback for older Chromium/account-manager builds that
    # do not expose page WebSocket targets.
    try:
        resp = requests.get(f"{CDP_ENDPOINT}/json/version", timeout=5)
        ws_url = resp.json().get("webSocketDebuggerUrl")
        endpoint = ws_url or CDP_ENDPOINT
        return playwright.chromium.connect_over_cdp(endpoint, timeout=15000)
    except Exception as error:
        raise RuntimeError(f"账号管家 CDP 连接失败（页面级与兼容模式均失败）：{error}") from error


def classify_admuse_login_state(url, visible_text, app_ready=False, login_required=False):
    """把妙思页面归为已登录、未登录或仍在加载，便于独立测试。"""
    normalized_url = str(url or "").lower()
    login_url_markers = ("/login", "passport", "open.weixin", "graph.qq", "xui.ptlogin")
    if any(marker in normalized_url for marker in login_url_markers):
        return "logged_out"
    lines = {line.strip().replace(" ", "") for line in str(visible_text or "").splitlines() if line.strip()}
    login_labels = {"登录", "登录/注册", "立即登录", "请先登录", "扫码登录", "QQ登录", "微信登录", "账号登录"}
    if login_required or lines.intersection(login_labels):
        return "logged_out"
    if app_ready:
        return "logged_in"
    return "unknown"


def is_admuse_application_ready(visible_text, visible_tabs=()):
    """识别灵感页完整框架；筛选后的排序项不一定仍包含“综合得分”。"""
    lines = {line.strip() for line in str(visible_text or "").splitlines() if line.strip()}
    tabs = {str(tab or "").strip() for tab in (visible_tabs or ()) if str(tab or "").strip()}
    has_navigation = "灵感" in lines and ("首页" in lines or "资产" in lines)
    has_image_tab = "图片" in tabs or "图片" in lines
    has_time_range = bool(lines.intersection({"近1天", "近3天", "近7天", "近30天"}))
    has_sort_order = any(line.endswith("降序") for line in lines)
    return has_navigation and has_image_tab and "行业" in lines and has_time_range and has_sort_order


def inspect_admuse_login(page):
    try:
        snapshot = page.evaluate("""
        () => {
            const body = document.body?.innerText || '';
            const lines = new Set(body.split(/\\r?\\n/).map((line) => line.trim()).filter(Boolean));
            const tabs = Array.from(document.querySelectorAll('.hotMaterialTab, .sc-ePDLzJ, button, [role="tab"]'))
                .filter((element) => element.offsetParent !== null)
                .map((element) => (element.innerText || '').trim());
            const normalizedLoginLabels = new Set([
                '登录', '登录/注册', '立即登录', '请先登录', '扫码登录',
                'QQ登录', '微信登录', '账号登录'
            ]);
            const visibleAuthControls = Array.from(
                document.querySelectorAll('button, a, [role="button"]')
            ).filter((element) => element.offsetParent !== null)
                .map((element) => (element.innerText || element.textContent || '').trim().replace(/\\s+/g, ''));
            const visibleAuthFrame = Array.from(document.querySelectorAll('iframe')).some((frame) => {
                if (frame.offsetParent === null) return false;
                const src = String(frame.src || '').toLowerCase();
                return src.includes('open.weixin') || src.includes('ptlogin') || src.includes('graph.qq');
            });
            return {
                url: location.href,
                text: body.slice(0, 6000),
                tabs,
                loginRequired: visibleAuthFrame || visibleAuthControls.some((text) => normalizedLoginLabels.has(text)),
            };
        }
        """)
    except Exception as exc:
        return {"state": "unknown", "url": getattr(page, "url", ""), "error": str(exc)}
    app_ready = is_admuse_application_ready(snapshot.get("text"), snapshot.get("tabs"))
    return {
        **snapshot,
        "appReady": app_ready,
        "state": classify_admuse_login_state(
            snapshot.get("url"),
            snapshot.get("text"),
            app_ready,
            snapshot.get("loginRequired"),
        ),
    }


def _visible_locator(frame, selectors):
    for selector in selectors:
        try:
            locator = frame.locator(selector)
            for index in range(min(locator.count(), 10)):
                candidate = locator.nth(index)
                if candidate.is_visible():
                    return candidate
        except Exception:
            continue
    return None


def _click_visible_label(frame, labels):
    try:
        clicked = frame.evaluate(
            r"""
            (labels) => {
                const expected = new Set(labels.map(
                    (label) => String(label || '').trim().replace(/\s+/g, ' ')
                ));
                const visible = (element) => Boolean(
                    element && element.offsetParent !== null
                    && element.getBoundingClientRect().width
                    && element.getBoundingClientRect().height
                );
                const interactiveSelector = [
                    'button', 'a', '[role="button"]',
                    'input[type="button"]', 'input[type="submit"]'
                ].join(',');
                const candidates = Array.from(document.querySelectorAll('*'))
                    .filter(visible)
                    .filter((element) => expected.has(
                        (element.innerText || element.textContent || element.value || '')
                            .trim().replace(/\s+/g, ' ')
                    ))
                    .map((element) => element.matches(interactiveSelector)
                        ? element
                        : element.closest(interactiveSelector) || element)
                    .filter((element, index, items) => items.indexOf(element) === index)
                    .sort((left, right) => {
                        const leftInteractive = left.matches(interactiveSelector) ? 0 : 1;
                        const rightInteractive = right.matches(interactiveSelector) ? 0 : 1;
                        return leftInteractive - rightInteractive
                            || left.querySelectorAll('*').length - right.querySelectorAll('*').length;
                    });
                const target = candidates[0];
                if (!target) return false;
                target.scrollIntoView({block: 'center', inline: 'center'});
                target.click();
                return true;
            }
            """,
            list(labels),
        )
        if clicked:
            return True
    except Exception:
        pass

    for label in labels:
        try:
            candidates = frame.get_by_text(label, exact=True)
            for index in range(min(candidates.count(), 10)):
                candidate = candidates.nth(index)
                if candidate.is_visible():
                    candidate.click(timeout=2000)
                    return True
        except Exception:
            continue
    return False


def open_admuse_qq_login(page, timeout=6):
    """打开妙思登录弹层并进入 QQ 登录，不依赖浏览器级 CDP。"""
    for frame in _auth_frames(page):
        if _click_visible_label(frame, ADMUSE_QQ_LOGIN_LABELS):
            return True

    opened = any(
        _click_visible_label(frame, ADMUSE_LOGIN_OPEN_LABELS)
        for frame in _auth_frames(page)
    )
    if not opened:
        return False

    deadline = time.monotonic() + max(0.5, timeout)
    while time.monotonic() < deadline:
        for frame in _auth_frames(page):
            if _click_visible_label(frame, ADMUSE_QQ_LOGIN_LABELS):
                return True
        time.sleep(0.25)
    return False


def _frame_location(frame):
    try:
        return str(frame.evaluate("() => location.href") or "").lower()
    except Exception:
        return str(getattr(frame, "url", "") or "").lower()


def _auth_frames(page):
    try:
        return list(page.frames)
    except Exception:
        return [page]


def _new_auth_page(page, original_pages):
    try:
        pages = list(page.context.pages)
    except Exception:
        return page
    candidates = [candidate for candidate in pages if candidate not in original_pages]
    if not candidates:
        return page
    return candidates[-1]


def load_admuse_qq_credentials():
    username = os.environ.get(ADMUSE_QQ_USERNAME_ENV, "").strip()
    password = os.environ.get(ADMUSE_QQ_PASSWORD_ENV, "")
    if username and password:
        return username, password, "environment"

    credentials_file = os.environ.get(
        ADMUSE_QQ_CREDENTIALS_FILE_ENV, DEFAULT_ADMUSE_QQ_CREDENTIALS_FILE
    ).strip()
    if not credentials_file or not os.path.isfile(credentials_file):
        return "", "", None

    content = None
    for encoding in ("utf-8-sig", "gbk"):
        try:
            with open(credentials_file, "r", encoding=encoding) as handle:
                content = handle.read()
            break
        except UnicodeDecodeError:
            continue
        except OSError:
            return "", "", None
    if content is None:
        return "", "", None

    labeled = {}
    unlabeled = []
    label_pattern = re.compile(
        r"^(qq账号|账号|用户名|username|user|密码|password)\s*[:：=]\s*(.+)$",
        re.IGNORECASE,
    )
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = label_pattern.match(line)
        if not match:
            unlabeled.append(line)
            continue
        key = match.group(1).lower()
        value = match.group(2).strip()
        if key in {"qq账号", "账号", "用户名", "username", "user"}:
            labeled["username"] = value
        else:
            labeled["password"] = value

    file_username = labeled.get("username", "")
    file_password = labeled.get("password", "")
    if not file_username and unlabeled:
        file_username = unlabeled[0]
    if not file_password and len(unlabeled) >= 2:
        file_password = unlabeled[1]
    if file_username and file_password:
        return file_username, file_password, "file"
    return "", "", None


def attempt_admuse_qq_password_login(page):
    """在妙思登录页发起 QQ 账号密码登录，不把凭据写入日志。"""
    username, password, source = load_admuse_qq_credentials()
    if not username or not password:
        # 以前这里静默 return False，用户只看到「等待账号管家恢复登录态」，
        # 完全不知道自动补登录其实是因为找不到凭据被跳过了。必须说出来。
        print(
            "LOGIN_STATUS|NO_CREDENTIAL|妙思|未找到 QQ 账号密码，无法自动补登录。"
            f"把凭据放到 {ADMUSE_QQ_CREDENTIALS_LOCAL}（两行：账号、密码），"
            "或设置环境变量 ADMUSE_QQ_CREDENTIALS_FILE 指向凭据文件"
        )
        return False

    focus_auth_page(page)
    source_label = "本机环境变量" if source == "environment" else "本地凭据文件"
    print(f"LOGIN_STATUS|AUTO|妙思|尝试 QQ 账号密码登录（凭据来自{source_label}）")

    auth_page = page
    try:
        original_pages = list(page.context.pages)
    except Exception:
        original_pages = [page]
    frames = _auth_frames(auth_page)
    current_url = str(getattr(auth_page, "url", "") or "").lower()
    if not any(marker in current_url for marker in ("passport", "xui.ptlogin", "graph.qq")):
        clicked_qq = open_admuse_qq_login(page)
        if clicked_qq:
            deadline = time.time() + 8
            while time.time() < deadline:
                auth_page = _new_auth_page(page, original_pages)
                auth_url = str(getattr(auth_page, "url", "") or "").lower()
                if auth_page is not page or any(
                    marker in auth_url for marker in ("passport", "xui.ptlogin", "graph.qq")
                ):
                    break
                if any(
                    any(marker in _frame_location(frame) for marker in (
                        "passport", "xui.ptlogin", "graph.qq"
                    ))
                    for frame in _auth_frames(auth_page)
                ):
                    break
                time.sleep(0.5)

        auth_page = _new_auth_page(page, original_pages)
        if auth_page is not page:
            try:
                auth_page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass

    frames = _auth_frames(auth_page)
    for frame in frames:
        _click_visible_label(frame, ("账号密码登录", "使用账号密码登录", "密码登录"))

    account_selectors = (
        "input[name='u']",
        "input[name='account']",
        "input[id='u']",
        "input[id*='account']",
        "input[placeholder*='账号']",
        "input[placeholder*='QQ号']",
        "input[type='text']",
    )
    password_selectors = (
        "input[name='p']",
        "input[name='password']",
        "input[id='p']",
        "input[id*='password']",
        "input[placeholder*='密码']",
        "input[type='password']",
    )
    account_input = None
    password_input = None
    submit_frame = None
    for frame in _auth_frames(auth_page):
        account_input = account_input or _visible_locator(frame, account_selectors)
        password_input = password_input or _visible_locator(frame, password_selectors)
        if account_input and password_input:
            submit_frame = frame
            break

    if not account_input or not password_input:
        print("LOGIN_STATUS|AUTO_WAIT|妙思|未找到账号密码表单，等待页面或手机验证")
        return False

    try:
        account_input.fill(username)
        password_input.fill(password)
    except Exception:
        print("LOGIN_STATUS|AUTO_WAIT|妙思|账号密码表单填充失败，等待人工登录")
        return False

    if submit_frame and _click_visible_label(submit_frame, ("登录", "登 录", "确认登录")):
        print("LOGIN_STATUS|AUTO_SUBMITTED|妙思|已提交 QQ 登录，等待手机验证或会话恢复")
    else:
        submit = _visible_locator(submit_frame or page, (
            "#login_button",
            "button[type='submit']",
            "input[type='submit']",
        ))
        if submit:
            try:
                submit.click(timeout=2000)
                print("LOGIN_STATUS|AUTO_SUBMITTED|妙思|已提交 QQ 登录，等待手机验证或会话恢复")
            except Exception:
                print("LOGIN_STATUS|AUTO_WAIT|妙思|未能点击登录按钮，等待人工登录")
                return False
        else:
            print("LOGIN_STATUS|AUTO_WAIT|妙思|未找到登录按钮，等待人工登录")
            return False
    return True


def ensure_admuse_logged_in(page, timeout=LOGIN_WAIT_SECONDS):
    """等待账号管家恢复妙思会话，并要求连续两次读到完整应用界面。"""
    deadline = time.time() + max(1, timeout)
    stable_checks = 0
    announced = False
    auto_login_attempts = 0
    next_auto_login_at = 0.0
    last_snapshot = {"state": "unknown", "url": getattr(page, "url", "")}
    print(f"LOGIN_STATUS|CHECKING|妙思|最多等待 {int(timeout)} 秒")
    while time.time() < deadline:
        last_snapshot = inspect_admuse_login(page)
        if last_snapshot.get("state") == "logged_in":
            stable_checks += 1
            if stable_checks >= 2:
                print(f"LOGIN_STATUS|READY|妙思|{last_snapshot.get('url', '')}")
                return True
        else:
            stable_checks = 0
            if last_snapshot.get("state") == "logged_out":
                now = time.time()
                if auto_login_attempts < 3 and now >= next_auto_login_at:
                    auto_login_attempts += 1
                    attempt_admuse_qq_password_login(page)
                    next_auto_login_at = now + 12
                if not announced:
                    announced = True
                    print("LOGIN_STATUS|REQUIRED|妙思|等待账号管家恢复登录态或手机验证")
                focus_auth_page(page)
        time.sleep(1.5)
    state = last_snapshot.get("state", "unknown")
    url = last_snapshot.get("url", getattr(page, "url", ""))
    raise RuntimeError(f"妙思登录态校验失败（状态={state}，页面={url}）。请在账号管家中重新登录妙思后重试")


def sync_session_cookies(context, session):
    """把账号管家中的最新 Cookie 同步给图片下载 Session。"""
    session.cookies.clear()
    cookies = context.cookies()
    for cookie in cookies:
        options = {"path": cookie.get("path") or "/"}
        if cookie.get("domain"):
            options["domain"] = cookie["domain"]
        session.cookies.set(cookie["name"], cookie["value"], **options)
    return len(cookies)

INDUSTRY_MAP = {
    "telecom": ("通讯和IT服务", "通信运营商"),
    "insurance": ("金融", "保险服务"),
    "lifestyle": ("生活服务", "生活优惠资讯"),
    "ecommerce": ("电商服务", "电商平台"),
}

DEFAULT_INDUSTRIES = ["telecom", "insurance", "lifestyle", "ecommerce"]
TIME_RANGE_MAP = {
    "1d": "近1天",
    "3d": "近3天",
    "7d": "近7天",
    "30d": "近30天",
    "all": None,
}
ADMUSE_SORT_OPTIONS = (
    "跑量得分降序",
    "曝光量降序",
    "点击率降序",
    "曝光转化率降序",
    "点击转化率降序",
    "综合得分降序",
)
DEFAULT_ADMUSE_SORT = ADMUSE_SORT_OPTIONS[0]

def parse_args():
    parser = argparse.ArgumentParser(
        description="AdMuse素材下载器（无人值守增强版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法:
  python admuse_download.py                              # 定时任务：跑全部四个行业，近7天，评分>40
  python admuse_download.py --industries telecom         # 仅通信运营商
  python admuse_download.py --industries insurance lifestyle  # 保险+生活优惠
  python admuse_download.py --industries ecommerce --keyword 快手  # 电商平台+关键词
  python admuse_download.py --time-range 30d --score 60  # 自定义时间范围和评分
  python admuse_download.py --dry-run                    # 仅打印配置不执行
        """
    )
    parser.add_argument(
        "--industries",
        nargs="+",
        choices=list(INDUSTRY_MAP.keys()),
        default=DEFAULT_INDUSTRIES,
        help=f"要抓取的行业（可多选）: {', '.join(INDUSTRY_MAP.keys())}，默认：全部四个"
    )
    parser.add_argument(
        "--keyword",
        default="快手",
        help="电商平台方向的搜索关键词，默认：快手（不会应用到其他行业）"
    )
    parser.add_argument(
        "--time-range",
        choices=list(TIME_RANGE_MAP.keys()),
        default="7d",
        help="时间范围：1d/3d/7d/30d/all，默认：7d"
    )
    parser.add_argument(
        "--score",
        type=int,
        default=SCORE_THRESHOLD,
        help=f"综合评分下限，默认：{SCORE_THRESHOLD}"
    )
    parser.add_argument(
        "--sort",
        choices=ADMUSE_SORT_OPTIONS,
        default=DEFAULT_ADMUSE_SORT,
        help=f"排序方式，默认：{DEFAULT_ADMUSE_SORT}"
    )
    parser.add_argument(
        "--max-downloads",
        type=int,
        default=MAX_DOWNLOADS,
        help=f"单次最大下载数，默认：{MAX_DOWNLOADS}"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印配置不执行下载"
    )
    return parser.parse_args()

def wait_for_element(page, js_condition, timeout=10, poll_interval=0.5):
    for _ in range(int(timeout / poll_interval)):
        result = page.evaluate(js_condition)
        if result:
            return result
        time.sleep(poll_interval)
    return None


def dismiss_admuse_blocking_overlays(page):
    """关闭会遮挡灵感页筛选器的产品公告。"""
    dismissed = _click_visible_label(page, ("我知道了",))
    if dismissed:
        time.sleep(1)
        print("  [页面提示] 已关闭遮挡筛选器的妙思公告")
    return dismissed


def get_current_time_range(page):
    return page.evaluate("""
    () => {
        const options = new Set(['近1天', '近3天', '近7天', '近30天']);
        for (const select of document.querySelectorAll('.spaui-select')) {
            if (select.offsetParent === null) continue;
            const selected = select.querySelector('.selection-single-text');
            const value = (selected?.innerText || '').trim();
            if (options.has(value)) return value;
        }
        return null;
    }
    """)

def click_expand_addon(page, name, max_attempts=3):
    for attempt in range(max_attempts):
        result = page.evaluate("""
        (name) => {
            const drop = document.querySelector('.selection-drop.with-addon');
            if (!drop || drop.offsetParent === null) return null;
            for (const li of drop.querySelectorAll('li')) {
                const nameEl = li.querySelector('.spaui-tree-name');
                if (nameEl && nameEl.innerText.trim() === name) {
                    const addon = li.querySelector('.spaui-tree-addon');
                    if (addon) {
                        addon.scrollIntoView({behavior: 'instant', block: 'center'});
                        const r = addon.getBoundingClientRect();
                        return [r.left + r.width/2, r.top + r.height/2];
                    }
                }
            }
            return null;
        }
        """, name)
        if result:
            page.mouse.click(result[0], result[1])
            return True
        time.sleep(0.5)
    return False

def is_checkbox_checked(page, name):
    return page.evaluate("""
    (name) => {
        const drop = document.querySelector('.selection-drop.with-addon');
        if (!drop || drop.offsetParent === null) return false;
        for (const cb of drop.querySelectorAll('.spaui-tree-checkbox')) {
            if (cb.innerText.trim() === name) {
                const li = cb.closest('li');
                if (li) {
                    const lc = li.className || '';
                    if (lc.split(' ').includes('checked')) {
                        return true;
                    }
                }
                const cc = cb.className || '';
                if (cc.split(' ').includes('spaui-checkbox-checked')) {
                    return true;
                }
                return false;
            }
        }
        return false;
    }
    """, name)

def click_checkbox(page, name, max_attempts=3, ensure_checked=True):
    for attempt in range(max_attempts):
        if ensure_checked and is_checkbox_checked(page, name):
            return True
        result = page.evaluate("""
        (name) => {
            const drop = document.querySelector('.selection-drop.with-addon');
            if (!drop || drop.offsetParent === null) return null;
            for (const cb of drop.querySelectorAll('.spaui-tree-checkbox')) {
                if (cb.innerText.trim() === name) {
                    cb.scrollIntoView({behavior: 'instant', block: 'center'});
                    const r = cb.getBoundingClientRect();
                    return [r.left + r.width/2, r.top + r.height/2];
                }
            }
            return null;
        }
        """, name)
        if result:
            page.mouse.click(result[0], result[1])
            time.sleep(0.6)
            if ensure_checked and is_checkbox_checked(page, name):
                return True
            if not ensure_checked:
                return True
        time.sleep(0.5)
    return False

def wait_for_drop(page, timeout=5):
    condition = """
    () => {
        const d = document.querySelector('.selection-drop.with-addon');
        return !!(d && d.offsetParent !== null && d.querySelectorAll('li').length > 3);
    }
    """
    return wait_for_element(page, condition, timeout=timeout) is not None

def close_detail_panel(page):
    closed = page.evaluate("""
    () => {
        const all = document.querySelectorAll('*');
        for (const el of all) {
            if (!el.offsetParent) continue;
            const r = el.getBoundingClientRect();
            if (r.width < 50 && r.height < 50 && r.width > 5) {
                const cls = el.className || '';
                const text = el.innerText?.trim() || '';
                if (cls.includes('close') || cls.includes('Close') ||
                    text === '×' || text === 'x' || text === '✕' || text === '关闭') {
                    const pr = el.closest('[class*="CardDetails"], [class*="cardDetails"], [class*="detail"], [class*="Detail"]');
                    if (pr) {
                        el.click();
                        return true;
                    }
                }
            }
        }
        return false;
    }
    """)
    if closed:
        time.sleep(1)
        return True
    if "/detail/" in page.url:
        page.evaluate("window.history.back()")
        time.sleep(2)
        return True
    return False

def ensure_list_page(page):
    close_detail_panel(page)
    page.evaluate("""
    () => {
        const el = document.querySelector('.flex-1.overflow-auto');
        if (el) el.scrollTop = 0;
        else window.scrollTo(0, 0);
    }
    """)
    time.sleep(1)

def click_image_tab(page, max_attempts=3):
    for attempt in range(max_attempts):
        clicked = page.evaluate("""
        () => {
            for (const b of document.querySelectorAll('.hotMaterialTab, .sc-ePDLzJ')) {
                if (b.innerText.trim() === '图片' && b.offsetParent !== null) {
                    b.click();
                    return true;
                }
            }
            return false;
        }
        """)
        if clicked:
            time.sleep(2)
            return True
        time.sleep(1)
    return False

def set_time_range(page, time_text, max_attempts=3):
    if time_text is None:
        print("  跳过时间范围筛选(不限)")
        return True

    dismiss_admuse_blocking_overlays(page)
    if get_current_time_range(page) == time_text:
        print(f"  [OK] 当前已是时间范围: {time_text}")
        return True

    for attempt in range(max_attempts):
        coord = page.evaluate(r"""
        () => {
            const timeOptions = new Set(['近1天', '近3天', '近7天', '近30天']);
            const selects = Array.from(document.querySelectorAll('.spaui-select, [role="combobox"]'));
            for (const s of selects) {
                if (s.offsetParent === null) continue;
                const selected = s.querySelector(
                    '.selection-single-text, [class*="single-text"], [class*="selected"]'
                );
                const text = (selected?.innerText || selected?.textContent || s.innerText || '')
                    .trim().replace(/\s+/g, '');
                if (!timeOptions.has(text)) continue;
                const r = s.getBoundingClientRect();
                if (r.width > 60 && r.height > 20 && r.y > 50 && r.y < 400) {
                    return [r.left + r.width/2, r.top + r.height/2];
                }
            }
            return null;
        }
        """)
        
        if not coord:
            print(f"  attempt {attempt+1}: time range selector not found")
            time.sleep(1)
            continue
        
        page.mouse.click(coord[0], coord[1])
        time.sleep(1)
        
        clicked = page.evaluate("""
        (timeText) => {
            const drop = Array.from(document.querySelectorAll('.selection-drop')).find(
                (candidate) => candidate.offsetParent !== null
                    && Array.from(candidate.querySelectorAll('li, [role="option"]')).some(
                        (item) => item.innerText.trim() === timeText
                    )
            );
            if (!drop) return false;
            
            for (const item of drop.querySelectorAll('li, [role="option"]')) {
                if (item.innerText.trim() === timeText && item.offsetParent !== null) {
                    item.scrollIntoView({behavior: 'instant', block: 'center'});
                    item.click();
                    return true;
                }
            }
            return false;
        }
        """, time_text)
        
        if clicked:
            time.sleep(1.5)
            if get_current_time_range(page) == time_text:
                print(f"  [OK] 时间范围设置为: {time_text}")
                return True
            print(f"  attempt {attempt+1}: 时间范围点击后未生效")
        else:
            print(f"  attempt {attempt+1}: '{time_text}' not found in dropdown")

        page.keyboard.press("Escape")
        time.sleep(1)
    
    print(f"  [X] 无法设置时间范围: {time_text}")
    return False


def get_current_sort_order(page):
    return page.evaluate("""
    (options) => {
        const normalized = new Map(options.map((option) => [option.replace(/\\s+/g, ''), option]));
        for (const select of document.querySelectorAll('.spaui-select, [role="combobox"]')) {
            if (select.offsetParent === null) continue;
            const selected = select.querySelector('.selection-single-text, [class*="single-text"], [class*="selected"]');
            const value = (selected?.innerText || selected?.textContent || select.innerText || '')
                .trim().replace(/\\s+/g, '');
            if (normalized.has(value)) return normalized.get(value);
        }
        return null;
    }
    """, list(ADMUSE_SORT_OPTIONS))


def set_sort_order(page, sort_text, max_attempts=3):
    target = sort_text if sort_text in ADMUSE_SORT_OPTIONS else DEFAULT_ADMUSE_SORT
    dismiss_admuse_blocking_overlays(page)
    if get_current_sort_order(page) == target:
        print(f"  [OK] 当前已是排序方式: {target}")
        return True

    for attempt in range(max_attempts):
        coord = page.evaluate("""
        (options) => {
            const normalized = new Set(options.map((option) => option.replace(/\\s+/g, '')));
            for (const select of document.querySelectorAll('.spaui-select, [role="combobox"]')) {
                if (select.offsetParent === null) continue;
                const selected = select.querySelector('.selection-single-text, [class*="single-text"], [class*="selected"]');
                const value = (selected?.innerText || selected?.textContent || select.innerText || '')
                    .trim().replace(/\\s+/g, '');
                if (!normalized.has(value)) continue;
                const rect = select.getBoundingClientRect();
                if (rect.width > 60 && rect.height > 20) {
                    return [rect.left + rect.width / 2, rect.top + rect.height / 2];
                }
            }
            return null;
        }
        """, list(ADMUSE_SORT_OPTIONS))
        if not coord:
            print(f"  attempt {attempt + 1}: sort selector not found")
            time.sleep(1)
            continue

        page.mouse.click(coord[0], coord[1])
        time.sleep(0.8)
        clicked = page.evaluate("""
        (target) => {
            const expected = target.replace(/\\s+/g, '');
            const candidates = document.querySelectorAll(
                '.selection-drop li, .selection-drop [role="option"], '
                + '.spaui-select-dropdown li, .spaui-select-dropdown [role="option"], '
                + '[role="listbox"] [role="option"]'
            );
            for (const item of candidates) {
                if (item.offsetParent === null) continue;
                const value = (item.innerText || item.textContent || '').trim().replace(/\\s+/g, '');
                if (value === expected) {
                    item.scrollIntoView({behavior: 'instant', block: 'center'});
                    item.click();
                    return true;
                }
            }
            return false;
        }
        """, target)
        if clicked:
            time.sleep(1.5)
            if get_current_sort_order(page) == target:
                print(f"  [OK] 排序方式设置为: {target}")
                return True
            print(f"  attempt {attempt + 1}: 排序点击后未生效")
        else:
            print(f"  attempt {attempt + 1}: 排序选项未找到: {target}")
        page.keyboard.press("Escape")
        time.sleep(0.8)

    print(f"  [X] 无法设置排序方式: {target}")
    return False

def close_industry_dropdown(page):
    drop_open = page.evaluate("() => { const d = document.querySelector('.selection-drop.with-addon'); return !!(d && d.offsetParent !== null); }")
    if not drop_open:
        return
    page.keyboard.press("Escape")
    time.sleep(0.8)
    drop_open = page.evaluate("() => { const d = document.querySelector('.selection-drop.with-addon'); return !!(d && d.offsetParent !== null); }")
    if not drop_open:
        return
    safe = page.evaluate("""
    () => {
        const sel = document.querySelector('.spaui-select');
        if (sel) {
            const r = sel.getBoundingClientRect();
            return [r.left + r.width/2, Math.max(r.top - 30, 10)];
        }
        return null;
    }
    """)
    if safe:
        page.mouse.click(safe[0], safe[1])
    else:
        page.keyboard.press("Escape")
    time.sleep(1)


def clear_industry_selection(page):
    """Clear the persisted industry choices before selecting one round."""
    cleared = page.evaluate("""
    () => {
        const clear = document.querySelector('.spaui-select-tree .spaui-select-clear');
        if (!clear) return false;
        clear.click();
        return true;
    }
    """)
    if cleared:
        time.sleep(0.8)
        print("  [OK] 已清空上一轮行业选择")
    return bool(cleared)


def set_industry(page, parent_name, child_name, max_attempts=3):
    ensure_list_page(page)
    click_image_tab(page)
    clear_industry_selection(page)

    for attempt in range(max_attempts):
        coord = page.evaluate("""
        () => {
            for (const s of document.querySelectorAll('.spaui-select')) {
                if (s.offsetParent !== null) {
                    const r = s.getBoundingClientRect();
                    if (r.width > 80 && r.height > 20 && r.y > 50 && r.y < 400) {
                        const el = document.elementFromPoint(r.left + r.width/2, r.top + r.height/2);
                        if (el && (el === s || s.contains(el))) {
                            return [r.left + r.width/2, r.top + r.height/2];
                        }
                    }
                }
            }
            return null;
        }
        """)
        if not coord:
            print(f"  attempt {attempt+1}: industry select blocked or not found")
            time.sleep(1)
            continue
        
        page.mouse.click(coord[0], coord[1])
        if not wait_for_drop(page, timeout=8):
            print(f"  attempt {attempt+1}: dropdown did not open")
            time.sleep(2)
            continue
        
        time.sleep(0.5)
        click_expand_addon(page, parent_name)
        time.sleep(1.5)
        
        names = page.evaluate("() => { const d = document.querySelector('.selection-drop.with-addon'); return d ? Array.from(d.querySelectorAll('.spaui-tree-name')).map(el => el.innerText.trim()) : []; }")
        
        if child_name in names:
            if not click_checkbox(page, child_name):
                print(f"  attempt {attempt+1}: 无法勾选 {child_name}")
                close_industry_dropdown(page)
                continue
            time.sleep(1)

            close_industry_dropdown(page)

            print(f"  [OK] Set industry: {child_name}")
            return True
        else:
            print(f"  {child_name} not found, available: {names[-5:]}")
            close_industry_dropdown(page)
            time.sleep(1)
    
    print(f"  [X] Failed to set industry: {child_name}")
    return False


def set_search_keyword(page, keyword, max_attempts=3):
    """Apply the creative keyword, including clearing a previous round's filter."""
    expected = (keyword or "").strip()
    for attempt in range(max_attempts):
        inputs = page.locator("input[placeholder='搜索创意关键词']")
        target = None
        for index in range(inputs.count()):
            candidate = inputs.nth(index)
            if candidate.is_visible():
                target = candidate
                break
        if target is None:
            print(f"  attempt {attempt+1}: keyword input not found")
            time.sleep(1)
            continue

        try:
            target.fill(expected)
            target.press("Enter")
            time.sleep(2)
            if target.input_value().strip() == expected:
                print(f"  [OK] 搜索关键词: {expected or '不限'}")
                return True
        except Exception as exception:
            print(f"  attempt {attempt+1}: keyword input failed: {exception}")
        time.sleep(1)

    print(f"  [X] 无法设置搜索关键词: {expected or '不限'}")
    return False


def recover_admuse_collection_session(
    page,
    context,
    session,
    parent_name,
    child_name,
    time_text,
    keyword,
    sort_text,
    reason,
):
    snapshot = inspect_admuse_login(page)
    if snapshot.get("state") == "logged_in":
        return False

    print(
        f"LOGIN_STATUS|LOST|妙思|{reason}，暂停采集并恢复登录"
        f"（状态={snapshot.get('state', 'unknown')}）"
    )
    ensure_admuse_logged_in(page, timeout=LOGIN_WAIT_SECONDS)
    page.goto(ADMUSE_IDEA_URL, wait_until="domcontentloaded", timeout=60000)
    ensure_admuse_logged_in(page, timeout=LOGIN_WAIT_SECONDS)

    if not click_image_tab(page):
        raise RuntimeError("妙思登录恢复后无法重新进入图片列表")
    if time_text is not None and not set_time_range(page, time_text):
        raise RuntimeError("妙思登录恢复后无法重新设置时间范围")
    if parent_name and child_name and not set_industry(page, parent_name, child_name):
        raise RuntimeError(f"妙思登录恢复后无法重新设置行业：{child_name}")
    if not set_search_keyword(page, keyword):
        raise RuntimeError(f"妙思登录恢复后无法重新设置搜索关键词：{keyword or '不限'}")
    if not set_sort_order(page, sort_text):
        raise RuntimeError(f"妙思登录恢复后无法重新设置排序方式：{sort_text}")

    cookie_count = sync_session_cookies(context, session)
    print(f"LOGIN_STATUS|RESTORED|妙思|登录已恢复，重新同步 {cookie_count} 个 Cookie 并继续采集")
    return True

def get_card_images(page):
    return page.evaluate("""
    () => {
        const results = [];
        const seenSrcs = new Set();
        for (const img of document.querySelectorAll('img')) {
            if (!img.src || img.src.startsWith('data:') || img.width < 100 || img.height < 100) continue;
            if (seenSrcs.has(img.src)) continue;
            const creativeItem = img.closest('.js-idea-creative-item');
            let el = creativeItem || img.parentElement;
            let score = null;
            while (el && el !== document.body) {
                const scoreMatch = (el.innerText || '').match(/(?:综合得分|跑量得分)\\s*([\\d.]+)/);
                if (scoreMatch) {
                    score = parseFloat(scoreMatch[1]);
                    break;
                }
                if (creativeItem) break;
                el = el.parentElement;
            }
            // 妙思新版图片卡片只保留 data-creative-id，不再渲染得分文字。
            // 此时仍可依靠页面的服务端排序采集，得分记录为 NA。
            if (creativeItem || score !== null) {
                seenSrcs.add(img.src);
                results.push({score, src: img.src});
            }
        }
        return results;
    }
    """)


def card_passes_score(score, score_threshold):
    """新版卡片不提供得分时不能本地过滤，但仍应按页面排序采集。"""
    return score is None or score > score_threshold


def scroll_admuse_list(page):
    """把素材列表向下滚一屏。

    步长按 SCROLL_STEP_RATIO 计算（默认 1 屏）。虚拟滚动列表一次跳太多屏会
    整段跳过未渲染的卡片，所以这里刻意用较小的步长。
    """
    page.evaluate(
        """
    (ratio) => {
        const el = document.querySelector('.flex-1.overflow-auto');
        const target = el || document.scrollingElement || document.documentElement;
        if (!target) return false;
        const step = Math.max(200, Math.round(target.clientHeight * ratio));
        if (el) el.scrollBy(0, step);
        else window.scrollBy(0, step);
        return true;
    }
    """,
        SCROLL_STEP_RATIO,
    )


def read_admuse_scroll_state(page):
    """读取列表容器滚动状态，用于区分"真的到底了"和"只是还没加载出来"。"""
    try:
        state = page.evaluate("""
        () => {
            const el = document.querySelector('.flex-1.overflow-auto');
            const target = el || document.scrollingElement || document.documentElement;
            if (!target) return null;
            const scrollTop = Number(target.scrollTop) || 0;
            const clientHeight = Number(target.clientHeight) || 0;
            const scrollHeight = Number(target.scrollHeight) || 0;
            return {scrollTop, clientHeight, scrollHeight};
        }
        """)
    except Exception:
        return None
    if not isinstance(state, dict):
        return None
    scroll_top = state.get("scrollTop") or 0
    client_height = state.get("clientHeight") or 0
    scroll_height = state.get("scrollHeight") or 0
    if scroll_height <= 0:
        at_bottom = False
    else:
        at_bottom = (
            scroll_height <= client_height + SCROLL_BOTTOM_TOLERANCE_PX
            or scroll_top + client_height >= scroll_height - SCROLL_BOTTOM_TOLERANCE_PX
        )
    return {
        "scrollTop": scroll_top,
        "clientHeight": client_height,
        "scrollHeight": scroll_height,
        "atBottom": at_bottom,
    }


def nudge_admuse_list(page):
    """贴底时的"轻推"：先回拉一屏、再回到最底。

    容器已经在最底时，scrollBy 是空操作，不会触发 scroll 事件，
    页面的懒加载因此永远等不到下一次触发，列表就卡死不动了。
    回拉再到底能制造两次真实的滚动事件，把加载重新唤醒。
    """
    try:
        page.evaluate("""
        () => {
            const el = document.querySelector('.flex-1.overflow-auto');
            const target = el || document.scrollingElement || document.documentElement;
            if (!target) return false;
            const max = Math.max(0, target.scrollHeight - target.clientHeight);
            target.scrollTop = Math.max(0, max - target.clientHeight);
            return true;
        }
        """)
    except Exception:
        return False
    time.sleep(0.6)
    try:
        page.evaluate("""
        () => {
            const el = document.querySelector('.flex-1.overflow-auto');
            const target = el || document.scrollingElement || document.documentElement;
            if (!target) return false;
            target.scrollTop = target.scrollHeight;
            return true;
        }
        """)
    except Exception:
        return False
    return True

def download_image(url, filepath, session):
    try:
        headers = {
            'Referer': 'https://admuse.qq.com/',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0',
        }
        resp = session.get(url, headers=headers, timeout=30, stream=True)
        if resp.status_code == 200:
            with open(filepath, 'wb') as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            return True, os.path.getsize(filepath)
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)

def dedup_by_md5(directory):
    seen = {}
    removed = 0
    for fname in sorted(os.listdir(directory)):
        fpath = os.path.join(directory, fname)
        if not os.path.isfile(fpath):
            continue
        h = hashlib.md5()
        with open(fpath, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                h.update(chunk)
        digest = h.hexdigest()
        if digest in seen:
            os.remove(fpath)
            removed += 1
            print(f"  dup removed: {fname} (same as {seen[digest]})")
        else:
            seen[digest] = fname
    return removed, len(seen)

def scroll_and_download(
    page,
    session,
    context,
    existing_files,
    downloaded_srcs,
    label,
    start_total,
    max_downloads,
    score_threshold,
    parent_name="",
    child_name="",
    time_text=None,
    keyword="",
    sort_text=DEFAULT_ADMUSE_SORT,
):
    downloaded_count = 0
    failed_count = 0
    scroll_count = 0
    no_new_rounds = 0
    last_cookie_refresh = 0

    page.evaluate("""
    () => {
        const el = document.querySelector('.flex-1.overflow-auto');
        if (el) el.scrollTop = 0;
        else window.scrollTo(0, 0);
    }
    """)
    time.sleep(1.5)

    # 容器高度是判断"列表还在加载"还是"真的到底"的关键依据
    last_scroll_height = 0
    no_growth_rounds = 0
    nudge_count = 0

    while True:
        snapshot = inspect_admuse_login(page)
        if snapshot.get("state") != "logged_in":
            if recover_admuse_collection_session(
                page,
                context,
                session,
                parent_name,
                child_name,
                time_text,
                keyword,
                sort_text,
                f"滚动第 {scroll_count} 轮检测到登录态异常",
            ):
                no_new_rounds = 0
        if scroll_count and scroll_count % 10 == 0:
            sync_session_cookies(context, session)
            print(f"  [登录态] 妙思会话正常，已刷新下载 Cookie（scroll={scroll_count}）")
        cards = get_card_images(page)
        new_cards = [c for c in cards if c['src'] not in downloaded_srcs]

        if not new_cards:
            no_new_rounds += 1
            print(f"  [{label}] No new cards (round {no_new_rounds}/{NO_NEW_ROUND_LIMIT})")
        else:
            no_new_rounds = 0
            for card in new_cards:
                if start_total + downloaded_count >= max_downloads:
                    print(f"  [{label}] Reached max {max_downloads} downloads.")
                    return downloaded_count, failed_count
                
                src = card['src']
                score = card['score']
                downloaded_srcs.add(src)

                if not card_passes_score(score, score_threshold):
                    continue

                url_part = src.split('/')[-1].split('?')[0][:40]
                clean = re.sub(r'[^\w\-]', '_', url_part)
                score_tag = score if score is not None else "NA"
                filename = f"[{label}]_score{score_tag}_{clean}.jpg"

                if filename in existing_files:
                    downloaded_count += 1
                    print(f"  [进度] 下载 {start_total + downloaded_count}/{max_downloads}（文件已存在）")
                    continue

                filepath = os.path.join(SAVE_DIR, filename)
                success, result = download_image(src, filepath, session)
                if not success and result in {"HTTP 401", "HTTP 403"}:
                    print(f"  [登录态] 图片请求返回 {result}，重新校验妙思登录并刷新 Cookie")
                    recover_admuse_collection_session(
                        page,
                        context,
                        session,
                        parent_name,
                        child_name,
                        time_text,
                        keyword,
                        sort_text,
                        f"图片请求返回 {result}",
                    )
                    sync_session_cookies(context, session)
                    success, result = download_image(src, filepath, session)
                if success:
                    downloaded_count += 1
                    existing_files.add(filename)
                    CAPTURE_ITEMS.append({
                        "file_name": filename,
                        "source_url": src,
                        "source_content_id": url_part,
                        "title": label,
                        "content_text": f"{label} score={score_tag}",
                        "raw": {"industry": label, "score": score_tag},
                    })
                    print(f"  [OK] [{label}#{downloaded_count}] score={score_tag} ({result//1024}KB) {filename}")
                    print(f"  [进度] 下载 {start_total + downloaded_count}/{max_downloads}")
                    if downloaded_count - last_cookie_refresh >= 50:
                        sync_session_cookies(context, session)
                        last_cookie_refresh = downloaded_count
                        print(f"  [登录态] 已刷新下载 Cookie（本轮 {downloaded_count} 张）")
                else:
                    failed_count += 1
                    print(f"  [X] [{label}] score={score_tag} failed: {result}")
                time.sleep(0.15)

        scroll_admuse_list(page)
        time.sleep(SCROLL_WAIT_SECONDS)
        scroll_count += 1
        scroll_state = read_admuse_scroll_state(page)

        # 容器高度是否还在增长 —— 比"有没有新卡片"更能反映加载是否停摆
        if scroll_state:
            grew = scroll_state["scrollHeight"] > last_scroll_height
            last_scroll_height = max(last_scroll_height, scroll_state["scrollHeight"])
        else:
            grew = False
        no_growth_rounds = 0 if grew else no_growth_rounds + 1

        if scroll_state:
            position = (
                f" 位置={scroll_state['scrollTop']}/{scroll_state['scrollHeight']}"
                f"{' 已到底' if scroll_state['atBottom'] else ''}"
                f"{' 高度增长' if grew else ''}"
            )
        else:
            position = " 位置=未知"

        print(
            f"  [{label}] scroll={scroll_count} seen={len(downloaded_srcs)} "
            f"dl={downloaded_count}{position}"
        )

        # 贴底且高度不再增长 = 加载可能停摆了。这时 scrollBy 已是空操作，
        # 必须回拉一屏再到底把懒加载重新唤醒，而不是就此收工。
        if scroll_state and scroll_state["atBottom"] and not grew:
            nudge_admuse_list(page)
            nudge_count += 1
            if nudge_count <= 3 or nudge_count % 5 == 0:
                print(
                    f"  [{label}] 已贴底但内容未增加，轻推一次以重新触发加载"
                    f"（第 {nudge_count} 次）"
                )

        # 只有"连续多轮没有新卡片"且"容器确实滚到底"且"高度多轮不再增长"
        # 三个条件同时成立，才认为列表真的采完了。
        if (
            no_new_rounds >= NO_NEW_ROUND_LIMIT
            and scroll_state
            and scroll_state["atBottom"]
            and no_growth_rounds >= NO_GROWTH_ROUND_LIMIT
        ):
            print(
                f"  [{label}] Done scrolling（连续 {no_new_rounds} 轮无新卡片、"
                f"已贴底、且容器高度连续 {no_growth_rounds} 轮未增长）."
            )
            break

        # 兜底：读不到滚动状态时不能无限滚下去
        if no_new_rounds >= NO_NEW_ROUND_HARD_LIMIT:
            print(
                f"  [{label}] 连续 {no_new_rounds} 轮无新卡片，停止采集"
                f"（未能确认已到底，可能是页面加载异常）."
            )
            break

        if scroll_count > MAX_SCROLL_ROUNDS:
            print(f"  [{label}] Max {MAX_SCROLL_ROUNDS} scrolls reached.")
            break

    return downloaded_count, failed_count

def main():
    args = parse_args()
    
    print("=" * 60)
    print("AdMuse素材下载器（无人值守增强版）")
    print("=" * 60)
    print(f"行业: {', '.join(args.industries)}")
    print(f"电商关键词: {args.keyword or '不限'}（仅电商行业生效，其他行业按不限处理）")
    print(f"时间范围: {args.time_range} ({TIME_RANGE_MAP[args.time_range] or '不限'})")
    print(f"排序方式: {args.sort}")
    print(f"评分阈值: > {args.score}（卡片未渲染得分时不做本地过滤）")
    print(f"最大下载: {args.max_downloads}")
    print(f"保存目录: {SAVE_DIR}")
    print("=" * 60)
    
    if args.dry_run:
        print("\n[dry-run] 配置检查完成，退出")
        return 0
    
    if not check_and_start_browser():
        print("无法启动浏览器，请手动启动账号管家")
        return 1
    
    browser = None
    initial_target_ids = None
    try:
        initial_target_ids = snapshot_target_ids(CDP_ENDPOINT)
        opened_target = open_platform_application(
            CDP_ENDPOINT,
            ADMUSE_ACCOUNT_MANAGER_PLATFORM,
            ADMUSE_ACCOUNT_HINT,
            ("admuse.qq.com", "graph.qq.com", "xui.ptlogin"),
            timeout=30,
        )
        if opened_target:
            print("[账号管家] 已打开妙思应用")
        else:
            print("[账号管家] 未找到妙思应用入口，将使用现有会话继续登录预检")

        with sync_playwright() as p:
            print(f"\n连接 Chrome (CDP {CDP_ENDPOINT})...")
            try:
                browser = connect_browser(p)
            except Exception as e:
                print(f"[X] 连接 Chrome 失败: {e}")
                print("请检查浏览器是否正常启动，或手动打开 admuse.qq.com 页面")
                return 1
            
            context = browser.contexts[0]
            
            bootstrap_page = None
            for pg in reversed(context.pages):
                if "admuse.qq.com" in pg.url:
                    bootstrap_page = pg
                    break

            if bootstrap_page is None:
                print("未找到妙思应用页，创建登录预检页...")
                bootstrap_page = context.new_page()
            else:
                print(f"Connected: {bootstrap_page.url}")
                maybe_bring_to_front(bootstrap_page)

            print("\n登录预检: 通过账号管家确认妙思会话...")
            bootstrap_page.goto(ADMUSE_IDEA_URL, wait_until="domcontentloaded", timeout=60000)
            ensure_admuse_logged_in(bootstrap_page)

            print("创建本次妙思采集标签页...")
            page = context.new_page()
            page.goto(ADMUSE_IDEA_URL, wait_until="domcontentloaded", timeout=60000)
            ensure_admuse_logged_in(page)
            print("  [OK] 妙思账号登录态有效")
            
            print("\nStep 1: Click 图片 tab")
            if click_image_tab(page):
                print("  Clicked 图片")
            time.sleep(2)
            
            print("\nStep 2: Set time range")
            time_text = TIME_RANGE_MAP[args.time_range]
            if not set_time_range(page, time_text):
                raise RuntimeError(f"妙思无法设置时间范围：{time_text or '不限'}")
            if not set_sort_order(page, args.sort):
                raise RuntimeError(f"妙思无法设置排序方式：{args.sort}")
            
            session = requests.Session()
            session.trust_env = False  # 忽略系统代理设置，避免代理软件未启动时采集下载全部失败
            cookie_count = sync_session_cookies(context, session)
            print(f"  已同步 {cookie_count} 个浏览器 Cookie 到下载会话")
            
            existing_files = set(os.listdir(SAVE_DIR))
            downloaded_srcs = set()
            total_dl = 0
            total_fail = 0
            
            print(f"\nExisting files: {len(existing_files)}")
            print(f"Score threshold: > {args.score}")
            
            for industry_key in args.industries:
                parent_name, child_name = INDUSTRY_MAP[industry_key]
                round_keyword = args.keyword if industry_key == "ecommerce" else ""
                print(f"\n===== Round: {child_name} =====")
                
                if set_industry(page, parent_name, child_name):
                    if not set_search_keyword(page, round_keyword):
                        raise RuntimeError(f"妙思无法设置搜索关键词：{round_keyword or '不限'}")
                    if not set_sort_order(page, args.sort):
                        raise RuntimeError(f"妙思无法设置排序方式：{args.sort}")
                    time.sleep(2)
                    close_detail_panel(page)
                    dl, fail = scroll_and_download(
                        page, session, context, existing_files, downloaded_srcs,
                        child_name, total_dl, args.max_downloads, args.score,
                        parent_name, child_name, time_text, round_keyword,
                        args.sort,
                    )
                    total_dl += dl
                    total_fail += fail
                    
                    if total_dl >= args.max_downloads:
                        print(f"\n已达到最大下载数 {args.max_downloads}，停止")
                        break
            
            print(f"\n=== Download done ===")
            print(f"Downloaded: {total_dl}, Failed: {total_fail}")
            print(f"Total files in {SAVE_DIR}: {len(os.listdir(SAVE_DIR))}")
            write_capture_items(OUTPUT_LAYOUT, CAPTURE_ITEMS)
            print("下载任务结束（未执行去重或分类；如需整理请使用“整理工坊”）")

            return 0
    
    except KeyboardInterrupt:
        print("\n\n用户中断")
        return 130
    
    except Exception as e:
        print(f"\n\n[X] 异常退出: {e}")
        import traceback
        traceback.print_exc()
        return 1

    finally:
        if initial_target_ids is not None:
            try:
                close_new_targets(
                    CDP_ENDPOINT,
                    initial_target_ids,
                    "妙思",
                    url_keywords=("admuse.qq.com", "graph.qq.com", "ptlogin"),
                )
            except Exception as cleanup_error:
                print(f"[页面清理] 妙思任务标签清理失败：{cleanup_error}")

if __name__ == "__main__":
    sys.exit(main())
