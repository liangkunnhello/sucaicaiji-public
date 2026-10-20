#!/usr/bin/env python
# -*- coding: utf-8 -*-
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)

import os
import time
import re
import hashlib
import base64
import math
import json
import warnings
from urllib.parse import urlparse
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r"urllib3 .* doesn't match a supported version",
    )
    import requests
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from direct_cdp import (
    CDPConnection,
    DirectBrowser,
    DirectPage,
    list_targets,
    open_target,
    open_target_in_context,
)
from output_layout import get_batch_layout, write_capture_items
from task_browser import task_target_scope

OUTPUT_LAYOUT = get_batch_layout("有米云")
SAVE_DIR = str(OUTPUT_LAYOUT["raw_dir"])
CDP_ENDPOINT = os.environ.get("MATERIAL_DOWNLOADER_CDP_ENDPOINT", "http://127.0.0.1:9223").rstrip("/")
BACKGROUND_MODE = os.environ.get("MATERIAL_DOWNLOADER_BACKGROUND_MODE", "true").strip().lower() not in {"0", "false", "no", "off"}
LOGIN_WAIT_SECONDS = max(30, int(os.environ.get("APPGROWING_LOGIN_WAIT_SECONDS", "180")))
LOGIN_SOLVER_MAX_ATTEMPTS = max(
    1, int(os.environ.get("APPGROWING_LOGIN_SOLVER_MAX_ATTEMPTS", "3"))
)
# 结果列表是懒渲染的：page.wait_for_load_state("networkidle") 只代表网络空闲，
# 不代表列表已经画出来。实测同一套筛选条件，列表出现耗时 4~15 秒不等；
# 读早了会拿到 0 个素材，进而被误判成「没有可下载素材」而整批失败。
RESULT_LOAD_TIMEOUT = max(5.0, float(os.environ.get("APPGROWING_RESULT_TIMEOUT", "40")))
RESULT_POLL_INTERVAL = max(0.2, float(os.environ.get("APPGROWING_RESULT_POLL_INTERVAL", "1.0")))
ACCOUNT_HINT = os.environ.get("APPGROWING_ACCOUNT_HINT", "设计部").strip()
JFBYM_API_URL = os.environ.get(
    "JFBYM_API_URL", "https://api.jfbym.com/api/YmServer/customApi"
).strip()
JFBYM_TYPE = "100018"
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_JFBYM_TOKEN_FILE = (
    WORKSPACE_ROOT / "data" / "shared" / "material_collections" / "有米云" / "云码Token.txt"
)
SORT_ALIASES = {
    "按视频曝光数降序": "按视频播放数降序",
}
RESULT_VIEWS = {"广告", "素材"}
DEFAULT_SORT_BY_VIEW = {
    "广告": "按视频播放数降序",
    "素材": "按关联广告创意数降序",
}
# 「素材类型」下拉是两级结构：父级 视频/图片/其他，叶子共 10 个。
# 实测（2026-09-25）：广告视图里结果全是视频卡，默认排序又是「按视频播放数降序」，
# 一旦再把类型筛成图片类，交集直接为空 —— 页面一条都不返回，
# 脚本随即报「没有找到可下载素材」整批失败。换成任何不依赖视频的排序即可正常出图。
VIDEO_MATERIAL_TYPES = {"视频", "竖视频", "全屏视频", "全部素材类型"}
VIDEO_ONLY_SORTS = {"按视频播放数降序", "按视频点赞数降序"}
# 与素材类型无关的兜底排序，任何类型都能出结果
FALLBACK_SORT = "按投放时间降序"


def resolve_sort(sort_text, result_view, material_type):
    """算出最终使用的排序，图片类素材自动避开视频类排序。

    实测（2026-09-25）：广告视图结果全是视频卡、默认排序又是「按视频播放数降序」，
    一旦把素材类型筛成图片类，交集为空 —— 页面一条都不返回，
    脚本随即报「没有找到可下载素材」整批失败。换成任何不依赖视频的排序即可正常出图。
    """
    resolved = str(sort_text or "").strip()
    if not resolved:
        resolved = DEFAULT_SORT_BY_VIEW.get(result_view, "")
    resolved = SORT_ALIASES.get(resolved, resolved)
    if material_type not in VIDEO_MATERIAL_TYPES and resolved in VIDEO_ONLY_SORTS:
        return FALLBACK_SORT
    return resolved
APPGROWING_HOME_URL = "https://appgrowing-cn.youcloud.com/"
APPGROWING_LOGIN_URL = "https://auth.youcloud.com/login?app_id=appgrowing"
APPGROWING_VIEWPORT_WIDTH = max(
    1024, int(os.environ.get("APPGROWING_VIEWPORT_WIDTH", "1280"))
)
APPGROWING_VIEWPORT_HEIGHT = max(
    720, int(os.environ.get("APPGROWING_VIEWPORT_HEIGHT", "900"))
)


def ensure_appgrowing_window(page):
    """Restore the account-manager window when its target was minimized."""
    target = getattr(page, "target", None)
    target_id = target.get("id") if isinstance(target, dict) else ""
    if not target_id:
        return False

    browser_connection = None
    try:
        version = requests.get(f"{CDP_ENDPOINT}/json/version", timeout=3).json()
        websocket_url = version.get("webSocketDebuggerUrl")
        if not websocket_url:
            return False
        browser_connection = CDPConnection(
            websocket_url, timeout=8, enable_page_domains=False
        )
        info = browser_connection.call(
            "Browser.getWindowForTarget", {"targetId": str(target_id)}
        )
        window_id = info.get("windowId")
        bounds = info.get("bounds") or {}
        if not window_id:
            return False

        changed = False
        if bounds.get("windowState") == "minimized":
            browser_connection.call(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "normal"}},
            )
            changed = True
            time.sleep(0.25)
            info = browser_connection.call(
                "Browser.getWindowForTarget", {"targetId": str(target_id)}
            )
            bounds = info.get("bounds") or {}

        width = int(bounds.get("width") or 0)
        height = int(bounds.get("height") or 0)
        if width < 800 or height < 600:
            browser_connection.call(
                "Browser.setWindowBounds",
                {
                    "windowId": window_id,
                    "bounds": {
                        "windowState": "normal",
                        "width": APPGROWING_VIEWPORT_WIDTH,
                        "height": APPGROWING_VIEWPORT_HEIGHT,
                    },
                },
            )
            changed = True
        if changed:
            print("[窗口尺寸] 已恢复有米云窗口，避免最小化页面导致登录页压缩")
        return changed
    except Exception as error:
        print(f"[窗口尺寸] 无法恢复有米云窗口，将继续尝试页面视口修复: {error}")
        return False
    finally:
        if browser_connection:
            browser_connection.close()


def _read_appgrowing_viewport(page):
    try:
        metrics = page.evaluate("""
        () => ({
            width: Math.round(window.innerWidth || 0),
            height: Math.round(window.innerHeight || 0),
            visualWidth: Math.round(window.visualViewport?.width || window.innerWidth || 0),
            visualHeight: Math.round(window.visualViewport?.height || window.innerHeight || 0),
        })
        """) or {}
    except Exception:
        return {}
    if not isinstance(metrics, dict):
        return {}
    result = {}
    for key in ("width", "height", "visualWidth", "visualHeight"):
        try:
            result[key] = int(metrics.get(key) or 0)
        except (TypeError, ValueError):
            result[key] = 0
    return result


def _appgrowing_viewport_is_usable(metrics):
    return (
        int(metrics.get("width") or 0) >= 800
        and int(metrics.get("height") or 0) >= 600
        and int(metrics.get("visualWidth") or 0) >= 800
        and int(metrics.get("visualHeight") or 0) >= 600
    )


def _appgrowing_viewport_label(metrics):
    return (
        f"{int(metrics.get('width') or 0)}x{int(metrics.get('height') or 0)}"
        f"/visual {int(metrics.get('visualWidth') or 0)}x"
        f"{int(metrics.get('visualHeight') or 0)}"
    )


def ensure_appgrowing_viewport(page, force=False, attempts=3):
    """Prevent account-manager/CDP tabs from inheriting a collapsed viewport.

    The account manager can leave a reused tab with an emulation viewport such
    as 408x42.  AppGrowing then renders only a thin strip in the user's Chrome
    window even though the window itself is full size.  Restore a desktop-sized
    viewport before inspecting, logging in, or creating a task tab.
    """
    ensure_appgrowing_window(page)
    original = _read_appgrowing_viewport(page)
    if _appgrowing_viewport_is_usable(original) and not force:
        return True

    connection = getattr(page, "connection", None)
    if connection:
        for _ in range(max(1, int(attempts))):
            try:
                connection.call("Emulation.clearDeviceMetricsOverride")
            except Exception:
                pass
            try:
                connection.call(
                    "Emulation.setPageScaleFactor", {"pageScaleFactor": 1}
                )
            except Exception:
                pass
            time.sleep(0.2)
            current = _read_appgrowing_viewport(page)
            if _appgrowing_viewport_is_usable(current):
                print(
                    "[页面尺寸] 已清除有米云遗留的压缩视口 "
                    f"（当前 {_appgrowing_viewport_label(current)}）"
                )
                return True

            try:
                connection.call("Emulation.setDeviceMetricsOverride", {
                    "width": APPGROWING_VIEWPORT_WIDTH,
                    "height": APPGROWING_VIEWPORT_HEIGHT,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                    "screenWidth": APPGROWING_VIEWPORT_WIDTH,
                    "screenHeight": APPGROWING_VIEWPORT_HEIGHT,
                    "scale": 1,
                })
            except Exception:
                time.sleep(0.2)
                continue
            time.sleep(0.25)
            current = _read_appgrowing_viewport(page)
            if _appgrowing_viewport_is_usable(current):
                print(
                    f"[页面尺寸] 已恢复有米云桌面视口 "
                    f"{APPGROWING_VIEWPORT_WIDTH}x{APPGROWING_VIEWPORT_HEIGHT} "
                    f"（原视口 {_appgrowing_viewport_label(original)}）"
                )
                return True

    # Playwright's regular CDP connection exposes this method; keep the
    # fallback for environments where the account-manager bridge is not used.
    setter = getattr(page, "set_viewport_size", None)
    if setter:
        try:
            setter({
                "width": APPGROWING_VIEWPORT_WIDTH,
                "height": APPGROWING_VIEWPORT_HEIGHT,
            })
            time.sleep(0.2)
        except Exception:
            pass
        current = _read_appgrowing_viewport(page)
        if _appgrowing_viewport_is_usable(current):
            print(
                f"[页面尺寸] 已恢复有米云桌面视口 "
                f"{APPGROWING_VIEWPORT_WIDTH}x{APPGROWING_VIEWPORT_HEIGHT}"
            )
            return True

    current = _read_appgrowing_viewport(page)
    print(
        "[页面尺寸] 有米云视口恢复后仍异常："
        f"{_appgrowing_viewport_label(current or original)}"
    )
    return False


def wait_for_stable_appgrowing_viewport(page, timeout=5, stable_reads=3):
    """Keep repairing until navigation no longer reapplies a tiny viewport."""
    deadline = time.monotonic() + max(0.5, float(timeout))
    consecutive = 0
    repairs = 0
    while time.monotonic() < deadline:
        current = _read_appgrowing_viewport(page)
        if _appgrowing_viewport_is_usable(current):
            consecutive += 1
            if consecutive >= max(1, int(stable_reads)):
                return True
        else:
            consecutive = 0
            if repairs < 3:
                ensure_appgrowing_viewport(page, force=True, attempts=2)
                repairs += 1
        time.sleep(0.25)
    current = _read_appgrowing_viewport(page)
    if _appgrowing_viewport_is_usable(current):
        return True
    print(
        "[页面尺寸] 有米云页面加载后仍未稳定："
        f"{_appgrowing_viewport_label(current)}"
    )
    return False


def maybe_bring_to_front(page):
    if not BACKGROUND_MODE:
        page.bring_to_front()

def _remember_target(target, persistent_target_ids):
    if persistent_target_ids is None or not target:
        return
    target_id = target.get("id")
    if target_id:
        persistent_target_ids.add(str(target_id))


def wait_appgrowing_document_ready(page, timeout=15000):
    deadline = time.monotonic() + timeout / 1000
    stable_reads = 0
    while time.monotonic() < deadline:
        try:
            state = page.evaluate("""
            () => ({
                readyState: document.readyState,
                bodyReady: Boolean(document.body),
            })
            """) or {}
        except Exception:
            state = {}
        if (
            state.get("bodyReady")
            and state.get("readyState") in {"interactive", "complete"}
        ):
            stable_reads += 1
            if stable_reads >= 3:
                return True
        else:
            stable_reads = 0
        time.sleep(0.25)
    return False


def refresh_appgrowing_page(page, timeout=15000):
    print("[登录复用] 发现旧有米云标签，先刷新页面再检查登录态")
    try:
        if getattr(page, "connection", None):
            page.connection.call("Page.reload", {"ignoreCache": False})
        else:
            page.reload(wait_until="domcontentloaded", timeout=timeout)
        if wait_appgrowing_document_ready(page, timeout=timeout):
            return True
    except Exception:
        pass
    print("[登录复用] 旧有米云标签刷新失败，将直接重新打开官网首页")
    return False


def inspect_existing_appgrowing_browser(targets):
    candidates = [
        target for target in targets
        if target.get("type") == "page"
        and target.get("webSocketDebuggerUrl")
        and urlparse(str(target.get("url", ""))).hostname == "appgrowing-cn.youcloud.com"
    ]
    for target in candidates:
        browser = None
        try:
            browser = _direct_browser_for_target(target)
            page = browser.contexts[0].pages[0]
            ensure_appgrowing_viewport(page)
            if not refresh_appgrowing_page(page):
                browser.close()
                browser = None
                continue
            login_states = []
            for attempt in range(5):
                login_states.append(is_appgrowing_application_page(page))
                if attempt < 4:
                    time.sleep(0.75)
            return browser, len(login_states) >= 3 and all(login_states[-3:])
        except Exception:
            pass
        if browser:
            browser.close()
    return None, False


def connect_browser(_playwright, persistent_target_ids=None):
    # 先确保 9223 就绪：不通会自动拉起账号管家并等待，避免连接被拒绝
    try:
        from cdp_guard import ensure_cdp, get_cdp_endpoint
        if not ensure_cdp():
            raise RuntimeError("9223 调试端口不可用，请开启账号管家调试端口后重试")
        global CDP_ENDPOINT
        CDP_ENDPOINT = get_cdp_endpoint()
    except ImportError:
        pass
    last_error = None
    for attempt in range(1, 4):
        try:
            try:
                resp = requests.get(f"{CDP_ENDPOINT}/json/version/", timeout=3)
            except Exception:
                resp = requests.get(f"{CDP_ENDPOINT}/json/version", timeout=3)
            resp.raise_for_status()
            break
        except Exception as error:
            last_error = error
            print(f"[连接浏览器] 第 {attempt}/3 次连接 9223 失败，2 秒后重试...")
            time.sleep(2)
    else:
        raise last_error
    targets = list_targets(CDP_ENDPOINT)
    existing_browser, logged_in = inspect_existing_appgrowing_browser(targets)
    if existing_browser:
        page = existing_browser.contexts[0].pages[0]
        _remember_target(getattr(page, "target", None), persistent_target_ids)
        if logged_in:
            print("[登录复用] 已复用现有有米云登录标签，本次不触发滑块验证")
        else:
            print("[官网直连] 旧标签已掉线，将直接在有米云网页恢复登录")
        return existing_browser

    login_target = next(
        (
            target for target in targets
            if target.get("type") == "page"
            and target.get("webSocketDebuggerUrl")
            and urlparse(str(target.get("url", ""))).hostname == "auth.youcloud.com"
            and "appgrowing" in str(target.get("url", "")).lower()
        ),
        None,
    )
    target = login_target or open_target(CDP_ENDPOINT, APPGROWING_HOME_URL, timeout=10)
    _remember_target(target, persistent_target_ids)
    print("[官网直连] 已直接打开有米云官网首页，不经过账号管家应用入口")
    return _direct_browser_for_target(target)

def download_image(url, filepath, session):
    try:
        headers = {"Referer": "https://appgrowing-cn.youcloud.com/"}
        resp = session.get(url, timeout=10, headers=headers)
        resp.raise_for_status()
        with open(filepath, "wb") as f:
            f.write(resp.content)
        return True
    except Exception as e:
        print(f"下载失败 {url}: {e}")
        return False

def dedup_by_md5(directory):
    seen = set()
    dup_count = 0
    for root, dirs, files in os.walk(directory):
        for filename in files:
            if not filename.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
                continue
            filepath = Path(root) / filename
            with open(filepath, "rb") as f:
                file_hash = hashlib.md5(f.read()).hexdigest()
            if file_hash in seen:
                os.remove(filepath)
                dup_count += 1
            else:
                seen.add(file_hash)
    return dup_count


def is_appgrowing_login_page(page):
    try:
        parsed = urlparse(str(page.url))
        if parsed.hostname == "auth.youcloud.com" and parsed.path.rstrip("/") == "/login":
            return True
        return bool(_find_email_input(page))
    except Exception:
        return False


def is_appgrowing_application_page(page):
    try:
        if urlparse(str(page.url)).hostname != "appgrowing-cn.youcloud.com":
            return False
        sign_in = page.query_selector("button.signin")
        return not (sign_in and _visible(sign_in))
    except Exception:
        return False


def open_appgrowing_login(page, timeout=20):
    ensure_appgrowing_viewport(page)
    if urlparse(str(page.url)).hostname != "appgrowing-cn.youcloud.com":
        login_ready = is_appgrowing_login_page(page)
        if login_ready:
            wait_for_stable_appgrowing_viewport(page)
        return login_ready
    sign_in = page.query_selector("button.signin")
    if sign_in and _trusted_click(page, sign_in):
        print("LOGIN_STATUS|OPENING|有米云|已点击右上角登录")
        deadline = time.time() + min(3, max(1, timeout))
        while time.time() < deadline:
            if is_appgrowing_login_page(page) or is_appgrowing_application_page(page):
                wait_for_stable_appgrowing_viewport(page)
                return True
            time.sleep(0.25)

    print("LOGIN_STATUS|OPENING|有米云|网页按钮未跳转，直接进入官方邮箱登录页")
    login_url = json.dumps(APPGROWING_LOGIN_URL)
    try:
        page.evaluate(f"() => location.assign({login_url})")
    except Exception:
        # Navigation can destroy the old execution context before CDP replies.
        pass
    deadline = time.time() + max(1, timeout)
    while time.time() < deadline:
        if is_appgrowing_login_page(page) or is_appgrowing_application_page(page):
            wait_for_stable_appgrowing_viewport(page)
            return True
        time.sleep(0.25)
    return False


def _direct_browser_for_target(target):
    connection = CDPConnection(target["webSocketDebuggerUrl"], timeout=15)
    page = DirectPage(connection, target)
    ensure_appgrowing_viewport(page)
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if page.url not in {"", "about:blank"}:
                break
        except Exception:
            pass
        time.sleep(0.25)
    return DirectBrowser(page)


def clone_appgrowing_tab_state(source_page, task_page):
    """Copy per-tab authentication state without logging or persisting its values."""
    state = source_page.evaluate("""
    () => ({
        localStorage: Object.fromEntries(Object.entries(localStorage)),
        sessionStorage: Object.fromEntries(Object.entries(sessionStorage)),
        windowName: window.name || '',
    })
    """)
    payload = json.dumps(state or {}, ensure_ascii=True)
    try:
        task_page.evaluate(f"""
        () => {{
            const state = {payload};
            for (const [key, value] of Object.entries(state.localStorage || {{}})) {{
                localStorage.setItem(key, value);
            }}
            for (const [key, value] of Object.entries(state.sessionStorage || {{}})) {{
                sessionStorage.setItem(key, value);
            }}
            if (state.windowName) window.name = state.windowName;
            location.reload();
        }}
        """)
    except Exception:
        # Navigation may destroy the old execution context before CDP replies.
        pass
    try:
        task_page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass


def open_appgrowing_task_target(source_page, timeout=10):
    """Open from the authenticated tab so Chromium clones its sessionStorage."""
    initial_ids = {
        str(item.get("id")) for item in list_targets(CDP_ENDPOINT) if item.get("id")
    }
    popup_url = json.dumps(APPGROWING_HOME_URL)
    try:
        source_page.evaluate(
            f"() => Boolean(window.open({popup_url}, '_blank'))"
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            target = next(
                (
                    item for item in list_targets(CDP_ENDPOINT)
                    if item.get("type") == "page"
                    and item.get("webSocketDebuggerUrl")
                    and str(item.get("id")) not in initial_ids
                    and (
                        "appgrowing" in str(item.get("url", "")).lower()
                        or str(item.get("url", "")) == "about:blank"
                    )
                ),
                None,
            )
            if target:
                return target
            time.sleep(0.1)
    except Exception:
        pass

    return open_target_in_context(
        CDP_ENDPOINT,
        APPGROWING_HOME_URL,
        source_page.target.get("id", ""),
        timeout=timeout,
    )


def create_appgrowing_task_page(browser):
    """Create a fresh collector tab after the bootstrap tab has authenticated."""
    if getattr(browser, "is_direct_cdp", False):
        source_page = browser.contexts[0].pages[0]
        target = open_appgrowing_task_target(source_page, timeout=10)
        task_browser = _direct_browser_for_target(target)
        page = task_browser.contexts[0].pages[0]
        ensure_appgrowing_viewport(page)
        clone_appgrowing_tab_state(source_page, page)
        browser.close()
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        wait_for_stable_appgrowing_viewport(page)
        return task_browser, page

    context = browser.contexts[0]
    page = context.new_page()
    page.goto(APPGROWING_HOME_URL, wait_until="domcontentloaded", timeout=60000)
    ensure_appgrowing_viewport(page)
    source_page = next(
        (candidate for candidate in context.pages if candidate is not page and is_appgrowing_application_page(candidate)),
        None,
    )
    if source_page:
        clone_appgrowing_tab_state(source_page, page)
    wait_for_stable_appgrowing_viewport(page)
    return browser, page


def _find_email_input(page):
    try:
        for item in page.query_selector_all("input.ymc-input__inner[type='text']"):
            if item.get_attribute("placeholder") == "请输入邮箱" and _visible(item):
                return item
    except Exception:
        pass
    return None


def load_jfbym_token():
    token = os.environ.get("JFBYM_TOKEN", "").strip()
    if token:
        return token, "environment"

    token_file = Path(
        os.environ.get("JFBYM_TOKEN_FILE", str(DEFAULT_JFBYM_TOKEN_FILE)).strip()
    )
    if not token_file.is_file():
        return "", None
    try:
        content = token_file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return "", None
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            if key.strip().lower() in {"token", "jfbym_token"}:
                line = value.strip()
        if line:
            return line, "file"
    return "", None


def parse_jfbym_trajectory(raw_result):
    points = [
        (float(x), float(y))
        for x, y in re.findall(
            r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)",
            str(raw_result or ""),
        )
    ]
    deduplicated = []
    for point in points:
        if not deduplicated or point != deduplicated[-1]:
            deduplicated.append(point)
    if len(deduplicated) < 4:
        raise ValueError("云码返回的轨迹点不足")
    return deduplicated


def request_jfbym_trajectory(image_bytes, token=None, http_client=requests):
    token = token or load_jfbym_token()[0]
    if not token:
        raise RuntimeError("未配置云码 Token")
    response = http_client.post(
        JFBYM_API_URL,
        json={
            "token": token,
            "type": JFBYM_TYPE,
            "image": base64.b64encode(image_bytes).decode("ascii"),
        },
        headers={"Content-Type": "application/json"},
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if int(payload.get("code", 0)) != 10000:
        raise RuntimeError(f"云码识别失败：{payload.get('msg', '未知错误')}")
    data = payload.get("data") or {}
    if data.get("code") not in (None, 0, "0"):
        raise RuntimeError(f"云码轨迹服务失败：{data.get('msg') or data.get('code')}")
    return parse_jfbym_trajectory(data.get("data"))


def _capture_slider_image(page):
    clip = page.evaluate("""
    () => {
        const element = document.querySelector('.ya-aliyun-captcha, .aliyun-captcha');
        if (!element) return null;
        const rect = element.getBoundingClientRect();
        return {
            x: Math.max(0, rect.left - 4),
            y: Math.max(0, rect.top - 4),
            width: rect.width + 8,
            height: rect.height + 8,
        };
    }
    """)
    if not clip or clip["width"] < 20 or clip["height"] < 20:
        raise RuntimeError("未找到有米云滑块区域")
    if getattr(page, "connection", None):
        captured = page.connection.call("Page.captureScreenshot", {
            "format": "png",
            "fromSurface": True,
            "captureBeyondViewport": False,
            "clip": {**clip, "scale": 1},
        })
        return base64.b64decode(captured["data"])
    return page.screenshot(type="png", clip=clip)


def _normalize_slider_trajectory(points, start_x, start_y, travel):
    first_x, first_y = points[0]
    last_x, last_y = points[-1]
    source_dx = last_x - first_x
    if abs(source_dx) < 5:
        distances = [0.0]
        for previous, current in zip(points, points[1:]):
            distances.append(
                distances[-1] + math.hypot(current[0] - previous[0], current[1] - previous[1])
            )
        total = max(1.0, distances[-1])
        progress_values = [distance / total for distance in distances]
    else:
        progress_values = [(point[0] - first_x) / source_dx for point in points]

    deviations = []
    for progress, point in zip(progress_values, points):
        baseline_y = first_y + (last_y - first_y) * progress
        deviations.append(point[1] - baseline_y)
    max_deviation = max(1.0, max(abs(value) for value in deviations))
    y_scale = min(1.0, 7.0 / max_deviation)

    mapped = []
    for progress, deviation in zip(progress_values, deviations):
        progress = max(-0.02, min(1.02, progress))
        mapped.append((start_x + travel * progress, start_y + deviation * y_scale))
    mapped[0] = (start_x, start_y)
    mapped[-1] = (start_x + travel + 2, start_y)
    return mapped


def _dispatch_mouse(page, event_type, x, y, pressed=False):
    if getattr(page, "connection", None):
        params = {
            "type": event_type,
            "x": x,
            "y": y,
            "pointerType": "mouse",
        }
        if event_type != "mouseMoved":
            params.update({
                "button": "left",
                "buttons": 1 if pressed else 0,
                "clickCount": 1,
            })
        elif pressed:
            params.update({"button": "left", "buttons": 1})
        page.connection.call("Input.dispatchMouseEvent", params)
        return
    if event_type == "mousePressed":
        page.mouse.down()
    elif event_type == "mouseReleased":
        page.mouse.up()
    else:
        page.mouse.move(x, y)


def _drag_slider_with_trajectory(page, trajectory):
    geometry = page.evaluate("""
    () => {
        const knob = document.querySelector('#aliyunCaptcha-sliding-slider, .slider-move');
        const track = document.querySelector('#aliyunCaptcha-sliding-body, .sliding');
        if (!knob || !track) return null;
        const knobRect = knob.getBoundingClientRect();
        const trackRect = track.getBoundingClientRect();
        return {
            startX: knobRect.left + knobRect.width / 2,
            startY: knobRect.top + knobRect.height / 2,
            travel: trackRect.width - knobRect.width,
        };
    }
    """)
    if not geometry or geometry["travel"] < 50:
        raise RuntimeError("有米云滑块尺寸异常")

    if len(trajectory) > 180:
        original_last = trajectory[-1]
        step = max(1, len(trajectory) // 180)
        trajectory = trajectory[::step]
        if trajectory[-1] != original_last:
            trajectory.append(original_last)
    path = _normalize_slider_trajectory(
        trajectory, geometry["startX"], geometry["startY"], geometry["travel"]
    )

    page.bring_to_front()
    start_x, start_y = path[0]
    for offset_x, offset_y in ((-42, 18), (-25, 7), (-11, -3), (0, 0)):
        _dispatch_mouse(page, "mouseMoved", start_x + offset_x, start_y + offset_y)
        time.sleep(0.045)
    _dispatch_mouse(page, "mousePressed", start_x, start_y, pressed=True)
    time.sleep(0.12)
    delay = max(0.006, min(0.022, 1.6 / max(1, len(path))))
    for index, (x, y) in enumerate(path[1:], 1):
        _dispatch_mouse(page, "mouseMoved", x, y, pressed=True)
        time.sleep(delay + (0.035 if index % 37 == 0 else 0))
    time.sleep(0.12)
    _dispatch_mouse(page, "mouseReleased", path[-1][0], path[-1][1])

    deadline = time.time() + 3
    while time.time() < deadline:
        passed = page.evaluate("""
        () => {
            const slider = document.querySelector('.ya-aliyun-captcha, .aliyun-captcha');
            if (!slider) return true;
            const text = (slider.innerText || '').trim();
            const className = String(slider.className || '').toLowerCase();
            return ['验证成功', '验证通过', '校验成功'].some((word) => text.includes(word))
                || ['success', 'passed', 'verified'].some((word) => className.includes(word));
        }
        """)
        if passed:
            return True
        time.sleep(0.15)
    return False


def try_jfbym_slider_login(page):
    token, _source = load_jfbym_token()
    if not token:
        return "not_configured"
    try:
        print("LOGIN_STATUS|JFBYM_REQUESTING|有米云|正在请求云码轨迹")
        image_bytes = _capture_slider_image(page)
        trajectory = request_jfbym_trajectory(image_bytes, token=token)
        print(f"LOGIN_STATUS|JFBYM_READY|有米云|收到 {len(trajectory)} 个轨迹点")
        if not _drag_slider_with_trajectory(page, trajectory):
            deadline = time.time() + 30
            while time.time() < deadline:
                if is_appgrowing_application_page(page):
                    print("LOGIN_STATUS|JFBYM_CONFIRMED|有米云|页面已确认登录成功")
                    return "submitted"
                state = appgrowing_login_form_state(page)
                if state.get("sliderPassed"):
                    _ensure_login_agreement(page)
                    login_button = _visible_text_element(page, "登录", "button")
                    if login_button and _trusted_click(page, login_button):
                        print("LOGIN_STATUS|SUBMITTED|有米云|云码轨迹验证通过，已点击登录")
                        return "submitted"
                time.sleep(0.5)
            return "verification_failed"
        _ensure_login_agreement(page)
        login_button = _visible_text_element(page, "登录", "button")
        if login_button and _trusted_click(page, login_button):
            print("LOGIN_STATUS|SUBMITTED|有米云|云码轨迹验证通过，已点击登录")
            return "submitted"
        return "submit_failed"
    except Exception as error:
        print(f"LOGIN_STATUS|JFBYM_FAILED|有米云|{error}")
        return "solver_failed"


def _visible(element):
    try:
        return bool(element.evaluate(
            "el => !!(el && el.getBoundingClientRect().width && el.getBoundingClientRect().height)"
        ))
    except Exception:
        return False


def _trusted_click(page, element):
    if not element or not _visible(element):
        return False
    try:
        box = element.evaluate("""
        el => {
            const rect = el.getBoundingClientRect();
            return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
        }
        """)
        page.bring_to_front()
        if getattr(page, "connection", None):
            page.connection.call("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": box["x"], "y": box["y"]
            })
            page.connection.call("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": box["x"], "y": box["y"],
                "button": "left", "clickCount": 1,
            })
            page.connection.call("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": box["x"], "y": box["y"],
                "button": "left", "clickCount": 1,
            })
        else:
            page.mouse.click(box["x"], box["y"])
        return True
    except Exception:
        return False


def _visible_text_element(page, text, selector="body *"):
    try:
        for element in page.query_selector_all(selector):
            if _visible(element) and element.inner_text().strip() == text:
                return element
    except Exception:
        pass
    return None


def appgrowing_login_form_state(page):
    try:
        return page.evaluate("""
        () => {
            const email = Array.from(
                document.querySelectorAll("input.ymc-input__inner[type='text']")
            ).find((element) => element.placeholder === '请输入邮箱');
            const password = document.querySelector("input[type='password']");
            const slider = document.querySelector('.ya-aliyun-captcha, .aliyun-captcha');
            const sliderText = (slider?.innerText || '').trim();
            const sliderClass = String(slider?.className || '').toLowerCase();
            const sliderVisible = Boolean(
                slider && slider.getBoundingClientRect().width && slider.getBoundingClientRect().height
            );
            const passedWords = ['验证成功', '验证通过', '校验成功'];
            return {
                emailReady: Boolean(email?.value),
                passwordReady: Boolean(password?.value),
                sliderReady: sliderVisible,
                sliderPassed: sliderVisible && (
                    passedWords.some((word) => sliderText.includes(word))
                    || ['success', 'passed', 'verified'].some((word) => sliderClass.includes(word))
                ),
                sliderText,
            };
        }
        """)
    except Exception:
        return {
            "emailReady": False,
            "passwordReady": False,
            "sliderReady": False,
            "sliderPassed": False,
            "sliderText": "",
        }


def _select_account_manager_login(page):
    candidates = []
    try:
        candidates = [item for item in page.query_selector_all("button.key-item") if _visible(item)]
    except Exception:
        pass
    if not candidates:
        return False
    selected = next(
        (item for item in candidates if ACCOUNT_HINT and ACCOUNT_HINT in item.inner_text()),
        candidates[0],
    )
    return _trusted_click(page, selected)


def _ensure_login_agreement(page):
    try:
        checkboxes = [
            item for item in page.query_selector_all("input[type='checkbox']") if _visible(item)
        ]
    except Exception:
        return False
    if not checkboxes:
        return False
    agreement = checkboxes[-1]
    if agreement.evaluate("el => Boolean(el.checked)"):
        return True
    if not _trusted_click(page, agreement):
        return False
    time.sleep(0.2)
    return bool(agreement.evaluate("el => Boolean(el.checked)"))


def prepare_appgrowing_login(page):
    if not is_appgrowing_login_page(page):
        return "not_required"

    wait_for_stable_appgrowing_viewport(page)
    page.bring_to_front()
    email_input = _find_email_input(page)
    if not email_input or not _visible(email_input):
        email_tab = _visible_text_element(page, "邮箱登录")
        if email_tab:
            _trusted_click(page, email_tab)
            time.sleep(0.5)

    state = appgrowing_login_form_state(page)
    if not (state.get("emailReady") and state.get("passwordReady")):
        email_input = _find_email_input(page)
        if email_input:
            _trusted_click(page, email_input)
            time.sleep(1)
        if _select_account_manager_login(page):
            time.sleep(1.5)
        state = appgrowing_login_form_state(page)

    if not (state.get("emailReady") and state.get("passwordReady")):
        return "account_required"
    if not _ensure_login_agreement(page):
        return "agreement_required"
    if state.get("sliderPassed"):
        return "ready_to_submit"
    if not state.get("sliderReady"):
        return "verification_loading"
    return "verification_required"


def ensure_appgrowing_logged_in(page, timeout=LOGIN_WAIT_SECONDS):
    ensure_appgrowing_viewport(page)
    if is_appgrowing_application_page(page):
        return True

    if not is_appgrowing_login_page(page):
        if not open_appgrowing_login(page):
            raise RuntimeError(f"有米云页面未登录且无法打开登录页（页面={page.url}）")
        if is_appgrowing_application_page(page):
            wait_for_stable_appgrowing_viewport(page)
            return True
        if not is_appgrowing_login_page(page):
            raise RuntimeError(f"有米云登录页跳转异常（页面={page.url}）")

    print(f"LOGIN_STATUS|CHECKING|有米云|最多等待 {int(timeout)} 秒")
    state = prepare_appgrowing_login(page)
    solver_status = "not_attempted"
    solver_attempts = 0
    next_solver_at = 0.0
    if state == "account_required":
        print("LOGIN_STATUS|ACCOUNT_REQUIRED|有米云|请在账号管家中确认“设计部”账号")
    elif state == "agreement_required":
        print("LOGIN_STATUS|AGREEMENT_REQUIRED|有米云|请勾选用户协议")
    elif state == "verification_required":
        solver_status = try_jfbym_slider_login(page)
        solver_attempts = 1
        next_solver_at = time.time() + 15
        if solver_status == "not_configured":
            print("LOGIN_STATUS|VERIFY_REQUIRED|有米云|未配置云码 Token，请手动拖动滑块")
        elif solver_status != "submitted":
            print("LOGIN_STATUS|VERIFY_REQUIRED|有米云|云码未通过，请手动完成滑块验证")

    deadline = time.time() + max(1, timeout)
    submitted = state == "verification_required" and solver_status == "submitted"
    submitted_at = time.time() if submitted else 0.0
    next_prepare_at = time.time() + 3
    next_viewport_check_at = 0.0
    while time.time() < deadline:
        now = time.time()
        if now >= next_viewport_check_at:
            current_viewport = _read_appgrowing_viewport(page)
            if not _appgrowing_viewport_is_usable(current_viewport):
                ensure_appgrowing_viewport(page, force=True, attempts=2)
            next_viewport_check_at = now + 2
        if urlparse(str(page.url)).hostname == "appgrowing-cn.youcloud.com":
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            time.sleep(0.5)
            if is_appgrowing_application_page(page):
                wait_for_stable_appgrowing_viewport(page)
                print(f"LOGIN_STATUS|READY|有米云|{page.url}")
                return True

        form = appgrowing_login_form_state(page)
        credentials_ready = form.get("emailReady") and form.get("passwordReady")
        if credentials_ready:
            _ensure_login_agreement(page)
        elif time.time() >= next_prepare_at:
            prepare_appgrowing_login(page)
            next_prepare_at = time.time() + 5

        if (
            submitted
            and time.time() - submitted_at >= 15
            and is_appgrowing_login_page(page)
        ):
            submitted = False

        if form.get("sliderPassed") and not submitted:
            login_button = _visible_text_element(page, "登录", "button")
            if login_button and _trusted_click(page, login_button):
                submitted = True
                submitted_at = time.time()
                print("LOGIN_STATUS|SUBMITTED|有米云|滑块验证已通过，已点击登录")
        elif (
            credentials_ready
            and form.get("sliderReady")
            and not form.get("sliderPassed")
            and not submitted
            and solver_attempts < LOGIN_SOLVER_MAX_ATTEMPTS
            and time.time() >= next_solver_at
        ):
            solver_status = try_jfbym_slider_login(page)
            solver_attempts += 1
            next_solver_at = time.time() + 15
            if solver_status == "submitted":
                submitted = True
                submitted_at = time.time()
        time.sleep(1)

    raise RuntimeError(
        f"有米云登录恢复超时（页面={page.url}）。请完成滑块验证后重试采集"
    )


def creative_page_ready(page):
    try:
        return bool(page.evaluate("""
        () => location.pathname.startsWith('/leaflet/list')
            && Boolean(document.querySelector('.ymc-filter-item'))
            && Boolean(document.querySelector('.ymc-advance-search__input'))
        """))
    except Exception:
        return False


def ensure_creative_page(page, timeout=30, poll_interval=0.25):
    if creative_page_ready(page):
        print(f"[创意入口] 已在创意筛选页：{page.url}")
        return True

    entry = None
    selectors = (
        "a.ymc-menu-item.menu-text[href='/leaflet/list']",
        "a.ymc-menu-item[href='/leaflet/list']",
        "a[href='/leaflet/list']",
    )
    for selector in selectors:
        try:
            for candidate in page.query_selector_all(selector):
                if candidate.inner_text().strip() == "创意":
                    entry = candidate
                    break
        except Exception:
            continue
        if entry:
            break

    if not entry:
        print(f"[创意入口] 当前页面未找到顶部“创意”入口：{page.url}")
        return False

    print(f"[创意入口] 当前页面不是筛选页，点击顶部“创意”：{page.url}")
    try:
        entry.evaluate("el => el.click()")
    except Exception as error:
        print(f"[创意入口] 点击失败：{error}")
        return False

    deadline = time.time() + max(1, timeout)
    while time.time() < deadline:
        if creative_page_ready(page):
            print(f"[创意入口] 创意筛选页已就绪：{page.url}")
            return True
        time.sleep(max(0.01, poll_interval))

    print(f"[创意入口] 等待创意筛选页超时：{page.url}")
    return False


def select_filter_options(page, filter_title, wanted_names):
    if not wanted_names:
        return True
    filter_div = page.query_selector(f"div.ymc-filter-item:has(.ymc-filter-item__title-content:text('{filter_title}'))")
    if not filter_div:
        print(f"未找到「{filter_title}」筛选区")
        return False

    multi_btn = next(
        (
            button for button in filter_div.query_selector_all("button.ymc-button")
            if button.inner_text().strip() == "多选" and _visible(button)
        ),
        None,
    )
    if multi_btn:
        multi_btn.evaluate("el => el.click()")
        time.sleep(0.4)

    wanted = set(wanted_names)
    found = set()
    changed = 0
    for item in filter_div.query_selector_all(".data-item"):
        label = item.query_selector("span.data-label")
        checkbox = item.query_selector("input.ymc-checkbox-input")
        if not label or not checkbox:
            continue
        name = label.inner_text().strip()
        desired = name in wanted
        if desired:
            found.add(name)
        current = bool(checkbox.evaluate("el => Boolean(el.checked)"))
        if current != desired:
            checkbox.evaluate("el => el.click()")
            changed += 1

    missing = [name for name in wanted_names if name not in found]
    if missing:
        for name in missing:
            print(f"  ⚠ 未找到：{name}")
        page.keyboard.press("Escape")
        return False

    confirm_btn = next(
        (
            button for button in filter_div.query_selector_all("button.ymc-button")
            if button.inner_text().strip() == "确认" and _visible(button)
        ),
        None,
    )
    if not confirm_btn:
        print(f"  ⚠ 「{filter_title}」未找到可见确认按钮")
        page.keyboard.press("Escape")
        return False
    confirm_btn.evaluate("el => el.click()")
    print(f"「{filter_title}」: 已精确设置 {len(found)} 个选项（变更 {changed} 个）")
    time.sleep(3.0)
    return True


def select_category_option(page, entry_text, option_text):
    # 入口文本会随选项变化，因此按固定位置定位：第 1 个是类型，第 2 个是比例。
    page.keyboard.press("Escape")
    time.sleep(0.2)
    page.keyboard.press("Escape")
    time.sleep(0.5)
    
    all_entries = page.query_selector_all("div.agd-category-select__label.el-popover__reference")
    if len(all_entries) < 2:
        print(f"下拉入口数量不足: {len(all_entries)}")
        return False
    
    if "素材类型" in entry_text:
        entry = all_entries[0]
    elif "素材比例" in entry_text:
        entry = all_entries[1]
    else:
        print(f"未知的entry_text: {entry_text}")
        return False

    if entry.inner_text().strip() == option_text:
        print(f"✓ 当前已是「{option_text}」")
        return True

    entry.click()
    time.sleep(1.0)

    popover = next(
        (
            item for item in page.query_selector_all(".agd-category-select__popover")
            if _visible(item)
        ),
        None,
    )
    if not popover:
        print(f"「{entry_text}」弹层未打开")
        page.keyboard.press("Escape")
        return False
    
    # 子级叶子节点在 .agd-category-select__item__sub 内
    sub_items = popover.query_selector_all(".agd-category-select__item__sub label.ymc-checkbox")
    
    target = None
    for cb in sub_items:
        lbl_span = cb.query_selector(".checkbox-label")
        text = (lbl_span.inner_text().strip() if lbl_span else cb.inner_text().strip())
        if text == option_text:
            target = cb
            break
    
    if target:
        changed = 0
        for checkbox in sub_items:
            desired = checkbox is target
            current = bool(checkbox.evaluate(
                "el => Boolean(el.querySelector('input.ymc-checkbox-input')?.checked)"
            ))
            if current != desired:
                checkbox.evaluate(
                    "el => el.querySelector('input.ymc-checkbox-input')?.click()"
                )
                changed += 1
        print(f"✓ 精确设置「{option_text}」（变更 {changed} 项）")
    else:
        print(f"未在子级选项中找到「{option_text}」")
        page.keyboard.press("Escape")
        return False

    confirm = popover.query_selector("button.el-button--primary")
    if not confirm or not _visible(confirm):
        print(f"「{entry_text}」未找到可见确定按钮")
        page.keyboard.press("Escape")
        return False
    confirm.evaluate("el => el.click()")
    time.sleep(2.0)
    return True


def reset_appgrowing_filters(page):
    clear_buttons = [
        button for button in page.query_selector_all(
            ".operation-wrapper button.ymc-button"
        )
        if button.inner_text().strip() == "清空"
    ]
    clear_button = next(
        (
            button for button in clear_buttons
            if _visible(button)
        ),
        None,
    )
    if not clear_button:
        if clear_buttons:
            print("✓ 页面当前没有残留筛选条件，无需清空")
            return True
        print("⚠ 未找到筛选清空按钮")
        return False
    clear_button.evaluate("el => el.click()")
    print("✓ 已重置页面残留筛选条件")
    time.sleep(2)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    return True


def select_result_view(page, view_text="广告"):
    """先切换结果视图；页面会在这里重置高级筛选，后续再重新应用筛选。"""
    buttons = page.query_selector_all(".agd-button-group button.agd-button-group__btn")
    target = None
    for button in buttons:
        text = button.inner_text().strip().splitlines()[0]
        if text == view_text:
            target = button
            break
    if not target:
        print(f"⚠ 未找到「{view_text}」视图按钮")
        return False

    class_name = target.get_attribute("class") or ""
    if "actived" in class_name:
        print(f"✓ 当前已是「{view_text}」视图")
        return True

    target.evaluate("el => el.click()")
    print(f"✓ 已切换到「{view_text}」视图")
    time.sleep(2)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    return True


def select_sort_option(page, sort_text):
    target_sort = SORT_ALIASES.get(sort_text, sort_text)
    sort_select = None
    selects = page.query_selector_all(".el-select.agd-select")
    for s in selects:
        current_value = s.evaluate(
            "el => el.querySelector('input')?.value || el.innerText || ''"
        )
        if any(x in current_value for x in ["排序", "降序", "相关性"]):
            sort_select = s
            break
    if not sort_select:
        print("未找到排序下拉")
        return False

    current_sort = sort_select.evaluate(
        "el => el.querySelector('input')?.value || ''"
    )
    if current_sort == target_sort:
        print(f"✓ 当前排序已是：{target_sort}")
        return True

    sort_select.click()
    time.sleep(0.8)

    items = page.query_selector_all(".el-select-dropdown__item")
    for it in items:
        is_visible = it.evaluate(
            "el => !!(el && el.offsetParent !== null)"
        )
        if is_visible and it.inner_text().strip() == target_sort:
            # 使用页面原生 click，兼容下拉层已打开或动画尚未结束的情况。
            it.evaluate("el => el.click()")
            print(f"✓ 选择排序：{target_sort}")
            time.sleep(0.6)
            return True
    page.keyboard.press("Escape")
    print(f"⚠ 未找到排序选项：{target_sort}")
    return False


def apply_advanced_filters(page, ratio, sort, material_type):
    if ratio != "全部素材比例":
        if not select_category_option(page, "全部素材比例", ratio):
            return False

    if not select_sort_option(page, sort):
        return False

    # 切换广告/素材视图会重置素材类型；类型必须作为最后一个筛选条件应用。
    if material_type != "全部素材类型":
        if not select_category_option(page, "全部素材类型", material_type):
            return False
    return True


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
    # 素材图位于 .agd-ad-material-v2__content 内(旧版为 __content-container；图片素材为 img,
    # 视频素材为 video[poster]),域名均为 creative-static-ag。
    # 排除应用默认图、媒体/平台图标等噪音。
    return page.evaluate("""() => {
        const urls = [];
        document.querySelectorAll(
            '.agd-ad-material-v2__content, .agd-ad-material-v2__content-container'
        ).forEach(c => {
            const img = c.querySelector('img');
            if (img && img.src && img.src.includes('creative-static-ag')) {
                urls.push(img.src);
                return;
            }
            const vid = c.querySelector('video[poster]');
            if (vid && vid.poster && vid.poster.includes('creative-static-ag')) {
                urls.push(vid.poster);
            }
        });
        return urls;
    }""")


def read_result_total(page):
    """读取结果总数（页面上的「共 N 条」）；读不到返回 None。"""
    try:
        raw = page.evaluate("""
        () => {
            const t = document.body.innerText || '';
            const m = t.match(/共\\s*([\\d,]+)\\s*条/);
            return m ? m[1].replace(/,/g, '') : '';
        }
        """)
    except Exception:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def wait_for_result_cards(page, timeout=None, interval=None):
    """等待结果列表真正渲染出素材卡片，返回可见的素材 URL 列表。

    不能用 page.wait_for_load_state("networkidle") 当作"列表已就绪"：
    那是网络空闲，SPA 在列表还没画出来时就已经空闲了。
    实测（2026-09-25）同一套筛选条件，列表出现要 4~15 秒不等；
    读早了得到 0 个，随后 goto_next_page 找不到分页按钮 → 被当成
    「已到最后一页」→ 直接判定没有素材而整批失败（当日 06:40 那次就是这样）。

    若页面已明确显示「共 0 条」则立即返回，不白等满超时。
    超时仍未出现卡片时返回空列表，由调用方决定如何报错。
    """
    wait_seconds = RESULT_LOAD_TIMEOUT if timeout is None else max(1.0, float(timeout))
    poll = RESULT_POLL_INTERVAL if interval is None else max(0.2, float(interval))
    deadline = time.monotonic() + wait_seconds
    probes = 0
    while True:
        urls = collect_page_materials(page)
        if urls:
            if probes:
                print(f"  结果列表已加载（轮询 {probes} 次，约 {probes * poll:.0f} 秒后出现）")
            return urls
        total = read_result_total(page)
        if total == 0:
            print("  页面显示「共 0 条」，筛选条件没有匹配到素材")
            return []
        if time.monotonic() >= deadline:
            if total is not None:
                print(f"  等待 {wait_seconds:.0f} 秒仍未出现素材卡片（页面显示共 {total} 条）")
            else:
                print(f"  等待 {wait_seconds:.0f} 秒仍未出现素材卡片，判定为无结果")
            return []
        probes += 1
        time.sleep(poll)


def goto_next_page(page, scroll_container):
    next_btn = page.query_selector(".el-pagination button.btn-next")
    if not next_btn:
        return False
    disabled = next_btn.get_attribute("disabled")
    if disabled is not None:
        return False
    next_btn.evaluate("el => el.click()")
    time.sleep(2)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    scroll_container.evaluate("el => el.scrollTo(0, 0)")
    time.sleep(1)
    return True


def collect_existing_url_tokens(base_dir):
    """已下载素材的 URL 尾段集合，用于跨采集去重。

    文件名形如 `[有米云]_20260928_0001_<url尾段>.jpg`，序号会随结果顺序变化，
    所以只取尾段做判据 —— 同一张图无论落在第几个位置都能被识别出来。
    """
    tokens = set()
    try:
        names = os.listdir(base_dir)
    except OSError:
        return tokens
    for name in names:
        if not name.startswith("[有米云]_"):
            continue
        parts = name.split("_", 3)
        if len(parts) == 4:
            tokens.add(parts[3].rsplit(".", 1)[0])
    return tokens


def main(
    media_names,
    traffic_names=None,
    keyword="",
    ratio="全部素材比例",
    max_downloads=500,
    sort="",
    mtype="全部素材类型",
    result_view="广告",
):
    traffic_names = traffic_names or []
    if result_view not in RESULT_VIEWS:
        raise RuntimeError(f"不支持的结果类型：{result_view}")
    sort = sort or DEFAULT_SORT_BY_VIEW[result_view]
    requested_sort = SORT_ALIASES.get(sort, sort)
    sort = resolve_sort(sort, result_view, mtype)
    if sort != requested_sort:
        print(
            f"⚠ 素材类型「{mtype}」不含视频，用「{requested_sort}」排序会返回 0 条结果"
            f"（实测确认），已自动改用「{sort}」"
        )
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
        "有米云",
        url_keywords=("appgrowing", "youcloud"),
    ) as persistent_target_ids, sync_playwright() as p:
        browser = connect_browser(p, persistent_target_ids)
        ctx = browser.contexts[0]
        page = next((pg for pg in ctx.pages if "appgrowing" in pg.url), None)
        if not page:
            raise RuntimeError("未找到有米云页面，请确认账号管家和 9223 调试端口可用")
        
        maybe_bring_to_front(page)
        wait_appgrowing_document_ready(page)
        time.sleep(2)

        ensure_appgrowing_logged_in(page)

        print("[任务页面] 创建本次有米云采集标签页")
        browser, page = create_appgrowing_task_page(browser)
        ensure_appgrowing_logged_in(page)

        if not ensure_creative_page(page):
            raise RuntimeError("无法进入有米云顶部“创意”筛选页，任务停止")
        
        page.evaluate("""
            document.querySelectorAll('[class*="survey"], [class*="Survey"], .PostHogSurvey, [id*="survey"]').forEach(e => e.remove());
        """)

        # 必须最先切换结果类型；AppGrowing 会在切换时重置素材类型、比例等高级筛选。
        if not select_result_view(page, result_view):
            raise RuntimeError(f"无法切换到{result_view}视图")
        if not reset_appgrowing_filters(page):
            raise RuntimeError("无法重置页面筛选条件")
        
        print(f"筛选条件：视图={result_view}, 媒体={media_names}, 流量平台={traffic_names}, 关键词={keyword}, 类型={mtype}, 比例={ratio}, 排序={sort}")
        
        if not select_filter_options(page, "流量平台", traffic_names):
            raise RuntimeError("流量平台设置失败")
        if not select_filter_options(page, "广告媒体", media_names):
            raise RuntimeError("广告媒体设置失败")
        
        if keyword:
            kw_input = page.query_selector("input.ymc-advance-search__input")
            if kw_input:
                kw_input.click()
                kw_input.fill(keyword)
                print(f"✓ 填入关键词：{keyword}")
                time.sleep(0.3)
                search_btn = page.query_selector("button.ymc-advance-search__button-search")
                if search_btn:
                    search_btn.evaluate("el => el.click()")
                    print(f"✓ 已点击搜索按钮")
                else:
                    kw_input.press("Enter")
                    print(f"✓ 已回车触发搜索")
                time.sleep(2)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
        
        if not apply_advanced_filters(page, ratio, sort, mtype):
            raise RuntimeError("高级筛选设置失败")
        
        time.sleep(2)
        print("筛选已应用，等待结果加载")
        
        scroll_container = page.query_selector("section.agd-container")
        if not scroll_container:
            raise RuntimeError("创意筛选页未找到素材滚动容器，页面结构可能已经变化")
        
        # 关键：先确认列表真的渲染出卡片再开始翻页。
        # 原来这里只等 networkidle，但 SPA 的网络早就空闲、列表却还没画出来，
        # 于是第 1 页读到 0 个、goto_next_page 又找不到分页按钮，
        # 就被当成「已到最后一页」直接判定没有素材（2026-09-25 06:40 的失败原因）。
        if not wait_for_result_cards(page):
            raise RuntimeError(
                "创意筛选页没有找到可下载素材。"
                "请检查筛选条件：图片类素材不能配「按视频播放数/点赞数」排序（会返回 0 条），"
                "也可尝试把结果类型换成「素材」视图。"
            )
        
        print(f"开始采集素材（上限 {max_downloads} 张，逐页翻页）...")
        collected = []          # 保序的素材原始 URL 列表
        seen_keys = set()       # 用于去重的 URL 关键部分(去掉签名参数)
        page_no = 1
        no_new_pages = 0
        
        while len(collected) < max_downloads:
            # 1. 当前页滚动到底,触发懒加载
            scroll_to_bottom(scroll_container)
            
            # 2. 收集本页素材图 URL
            page_urls = collect_page_materials(page)
            new_cnt = 0
            for url in page_urls:
                key = url.split("?")[0]
                if key not in seen_keys:
                    seen_keys.add(key)
                    collected.append(url)
                    new_cnt += 1
            print(f"第 {page_no} 页：本页 {len(page_urls)} 个，新增 {new_cnt} 个，累计 {len(collected)} 个")

            if new_cnt == 0:
                no_new_pages += 1
                if no_new_pages >= 3:
                    print("连续 3 页没有新增素材，停止翻页")
                    break
            else:
                no_new_pages = 0
            
            if len(collected) >= max_downloads:
                break
            
            # 3. 翻到下一页
            if not goto_next_page(page, scroll_container):
                print("已到最后一页，无法继续翻页")
                break
            page_no += 1
        
        if len(collected) > max_downloads:
            collected = collected[:max_downloads]
        if not collected:
            raise RuntimeError("创意筛选页没有找到可下载素材，请检查筛选条件或页面结构")
        print(f"共采集 {len(collected)} 个素材,开始下载...")
        
        # 同一天同一平台共用一个目录，会跑多次（不同方向/关键词）。
        # 文件名里带序号，一旦两次的结果顺序不同，同一张图就会被存成两个重复文件，
        # 所以按 URL 尾段（与序号无关）跨采集去重。
        existing_tokens = collect_existing_url_tokens(base_dir)
        capture_items = []
        skipped = 0
        for idx, url in enumerate(collected, 1):
            clean_url = re.sub(r'[^\w\-.]', '_', url.split('/')[-1].split('?')[0])
            token = clean_url[:60]
            filename = f"[有米云]_{today}_{idx:04d}_{token}.jpg"
            filepath = base_dir / filename
            
            if token in existing_tokens:
                skipped += 1
                print(f"跳过（已存在）：{token}")
                time.sleep(0.1)
                continue
            
            print(f"下载 {idx}/{len(collected)}: {url[:80]}...")
            if download_image(url, filepath, session):
                existing_tokens.add(token)
                capture_items.append({
                    "file_name": filename,
                    "source_url": url,
                    "source_content_id": url.split("?")[0].rsplit("/", 1)[-1],
                    "title": keyword or token,
                    "content_text": keyword,
                    "raw": {"media": media_names, "traffic": traffic_names,
                            "ratio": ratio, "material_type": mtype, "result_view": result_view},
                })
                print(f"✓ 已保存：{filename}")
            time.sleep(0.3)
        
        if skipped:
            print(f"本次跳过已存在的素材 {skipped} 个（同一天重复采集不会重复下载）")
        write_capture_items(OUTPUT_LAYOUT, capture_items)
        print(f"\n下载完成，保存至：{base_dir}")
        print("下载任务结束（未执行去重或分类；如需整理请使用“整理工坊”）")


def run_capture_with_retry(
    media_names,
    traffic_names,
    keyword,
    ratio,
    max_downloads,
    sort,
    mtype,
    result_view,
    attempts=2,
    retry_delay=3,
):
    attempts = max(1, attempts)
    for attempt in range(1, attempts + 1):
        try:
            return main(
                media_names,
                traffic_names,
                keyword,
                ratio,
                max_downloads,
                sort,
                mtype,
                result_view,
            )
        except Exception as error:
            if attempt >= attempts:
                print(f"CAPTURE_ERROR|{type(error).__name__}|{error}", flush=True)
                raise
            print(
                f"CAPTURE_RETRY|{attempt}/{attempts - 1}|"
                f"{type(error).__name__}|{error}|{retry_delay} 秒后重试",
                flush=True,
            )
            time.sleep(max(0, retry_delay))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="有米云 AppGrowing 素材下载器")
    parser.add_argument("--media", default="", help="广告媒体，逗号分隔")
    parser.add_argument("--traffic", default="", help="流量平台，逗号分隔")
    parser.add_argument("--keyword", default="", help="搜索关键词")
    parser.add_argument("--ratio", default="全部素材比例", help="素材比例")
    parser.add_argument("--type", dest="mtype", default="全部素材类型", help="素材类型")
    parser.add_argument("--view", choices=sorted(RESULT_VIEWS), default="广告", help="结果类型")
    parser.add_argument("--max", type=int, default=500, help="下载数量上限")
    parser.add_argument("--sort", default="", help="排序方式；留空时按结果类型使用默认值")
    args = parser.parse_args()
    
    media_list = [m.strip() for m in args.media.split(",") if m.strip()]
    traffic_list = [t.strip() for t in args.traffic.split(",") if t.strip()]
    run_capture_with_retry(
        media_list,
        traffic_list,
        args.keyword,
        args.ratio,
        args.max,
        args.sort,
        args.mtype,
        args.view,
    )
