#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""内部系统的目标标签页直连备用通道。

只连接 内部系统当前标签页，不初始化账号管家中的其他页面、扩展或
Playwright BrowserContext，因此不会关闭浏览器，也不会复制登录令牌。
"""

import argparse
import datetime
import io
import json
import os
import sys
import time
from pathlib import Path

from urllib.parse import urlparse

import requests
from websockets.sync.client import connect

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer,
        encoding="utf-8",
        errors="replace",
        line_buffering=True,
    )


CDP_ENDPOINT = os.environ.get("MATERIAL_DOWNLOADER_CDP_ENDPOINT", "http://127.0.0.1:9223").rstrip("/")
# 内部系统地址：不同环境部署在自己的域名上，用环境变量注入，不要写死在代码里。
INTERNAL_URL = os.environ.get(
    "MATERIAL_DOWNLOADER_INTERNAL_URL",
    "https://internal-center.example.com/login#/home/video-material-consumption",
)
INTERNAL_URL_HOST = urlparse(INTERNAL_URL).hostname or ""
INTERNAL_HOST_SUFFIX = ".".join(INTERNAL_URL_HOST.split(".")[-2:]) if INTERNAL_URL_HOST else ""
BACKGROUND_MODE = os.environ.get("MATERIAL_DOWNLOADER_BACKGROUND_MODE", "true").strip().lower() not in {
    "0", "false", "no", "off"
}


class CDPError(RuntimeError):
    pass


class CDPClient:
    def __init__(self, websocket_url):
        self.socket = connect(
            websocket_url,
            open_timeout=5,
            close_timeout=1,
            max_size=None,
        )
        self.next_id = 0

    def close(self):
        try:
            self.socket.close()
        except Exception:
            pass

    def call(self, method, params=None, timeout=12):
        self.next_id += 1
        command_id = self.next_id
        payload = {"id": command_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.socket.send(json.dumps(payload, ensure_ascii=False))

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CDPError(f"CDP 命令超时：{method}")
            message = json.loads(self.socket.recv(timeout=remaining))
            if message.get("id") != command_id:
                continue
            if "error" in message:
                error = message["error"]
                raise CDPError(
                    f"{method} 执行失败：{error.get('message', error)}"
                )
            return message.get("result", {})

    def evaluate(self, expression, timeout=12):
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            raise CDPError(details.get("text", "页面脚本执行失败"))
        remote = result.get("result", {})
        return remote.get("value")


def _visible_element_script(selector, text=None, exact=True):
    selector_json = json.dumps(selector, ensure_ascii=False)
    text_json = json.dumps(text, ensure_ascii=False)
    exact_json = "true" if exact else "false"
    return f"""
    (() => {{
      const selector = {selector_json};
      const expected = {text_json};
      const exact = {exact_json};
      const visible = element => Boolean(
        element.offsetWidth || element.offsetHeight || element.getClientRects().length
      );
      const candidates = [...document.querySelectorAll(selector)]
        .filter(visible)
        .filter(element => {{
          if (expected === null) return true;
          const value = (element.innerText || element.textContent || "").trim();
          return exact ? value === expected : value.includes(expected);
        }})
        .sort((left, right) => left.children.length - right.children.length);
      return candidates[0] || null;
    }})()
    """


def click_element(page, selector, text=None, exact=True):
    element_script = _visible_element_script(selector, text, exact)
    return bool(page.evaluate(f"""
    (() => {{
      const element = {element_script};
      if (!element) return false;
      element.scrollIntoView({{block: "center", inline: "center"}});
      element.click();
      return true;
    }})()
    """))


def press_escape(page):
    page.call(
        "Input.dispatchKeyEvent",
        {
            "type": "keyDown",
            "key": "Escape",
            "code": "Escape",
            "windowsVirtualKeyCode": 27,
            "nativeVirtualKeyCode": 27,
        },
    )
    page.call(
        "Input.dispatchKeyEvent",
        {
            "type": "keyUp",
            "key": "Escape",
            "code": "Escape",
            "windowsVirtualKeyCode": 27,
            "nativeVirtualKeyCode": 27,
        },
    )


def set_filter_date(page, filter_date):
    """把查询日期写进 Element 的日期范围控件。

    ⚠ 这里曾是「从失败任务再次执行」永远失败的根因，改回写 input.value 会复发：
    账号管家标签页是长期存活的页面，Element 的 daterange 控件把「显示值」和
    「内部模型」分开存。只写 input.value（哪怕再派发 input/change/blur 事件）
    只改到显示，查询仍会用页面加载时的旧日期 —— 于是 verify_applied_date 读到的
    表格日期始终是页面默认的「昨天」，与目标日期不符，脚本主动中止导出。
    这解释了为什么普通定时任务能跑（目标日期恰好就是页面默认的「昨天」），
    而任何历史日期的重跑都会失败。

    正确做法是直接驱动 Vue 组件：ElDatePicker 的 emitInput() 会同时更新 v-model
    与显示值，查询才真正按目标日期执行。
    """
    try:
        datetime.date.fromisoformat(filter_date)
    except ValueError as error:
        raise CDPError(f"内部系统筛选日期格式无效：{filter_date}") from error
    value = json.dumps(filter_date, ensure_ascii=False)
    result = page.evaluate(f"""
    (() => {{
      const target = {value};
      const editor = document.querySelector(".el-date-editor--daterange")
        || [...document.querySelectorAll(".el-date-editor")]
             .find(element => element.querySelector("input[placeholder='开始日期']"));
      if (!editor) return {{ ok: false, reason: "未找到日期范围控件" }};
      const parts = target.split("-").map(Number);
      const date = new Date(parts[0], parts[1] - 1, parts[2]);
      const vm = editor.__vue__;
      if (vm && typeof vm.emitInput === "function") {{
        // 首选：走组件自身入口，同时刷新 v-model 与显示值
        vm.emitInput([date, date]);
      }} else if (vm) {{
        vm.$emit("input", [date, date]);
      }} else {{
        // 兜底：拿不到组件实例时退回写显示值（旧行为，可能不生效，交由下面的校验报错）
        const setter = Object.getOwnPropertyDescriptor(
          HTMLInputElement.prototype, "value"
        ).set;
        for (const input of editor.querySelectorAll("input")) {{
          setter.call(input, target);
          input.dispatchEvent(new Event("input", {{ bubbles: true }}));
          input.dispatchEvent(new Event("change", {{ bubbles: true }}));
          input.dispatchEvent(new Event("blur", {{ bubbles: true }}));
        }}
      }}
      return {{ ok: true }};
    }})()
    """)
    if not result or not result.get("ok"):
        raise CDPError(
            f"内部系统筛选日期设置失败：{(result or {}).get('reason', '未知原因')}"
        )
    time.sleep(0.5)
    values = page.evaluate(
        "(() => [...document.querySelectorAll('.el-range-input')]"
        ".map(input => input.value.trim()))()"
    )
    if values != [filter_date, filter_date]:
        raise CDPError(
            f"内部系统筛选日期设置失败：期望 {filter_date}，页面显示 {values}"
        )
    print(f"✓ 已固定查询日期：{filter_date} 至 {filter_date}")


def clear_product_filter(page):
    """清除「产品」筛选中残留的已选项，返回被清除的产品名列表。

    账号管家标签页是长期存活的浏览器上下文，产品筛选不会被上一轮任务重置。
    一旦页面上残留了某个产品，而该产品在查询日期没有消耗数据，接口会稳定
    返回 0 条，导出文件只有表头，最终表现为“本次没有采集到素材”。
    """
    removed = page.evaluate("""
    (() => {
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
    })()
    """)
    if removed is None:
        print("  ⚠ 未找到「产品」筛选项，跳过残留筛选清理")
        return []
    if removed:
        print(f"✓ 已清除残留的产品筛选：{'、'.join(removed)}")
    else:
        print("产品筛选为空，无需清理")
    return removed


def read_result_total(page):
    """读取查询结果总条数；取不到时返回 None。"""
    return page.evaluate("""
    (() => {
      const visible = element => Boolean(
        element.offsetWidth || element.offsetHeight || element.getClientRects().length
      );
      const text = element => (element.innerText || element.textContent || "").trim();
      const node = [...document.querySelectorAll(".el-pagination__total")]
        .filter(visible)[0];
      if (!node) return null;
      const match = text(node).match(/(\\d+)/);
      return match ? Number(match[1]) : null;
    })()
    """)


def read_applied_date(page):
    """读取表格「日期」列首行的值 —— 这是本次查询**真正生效**的日期。

    列顺序可被「自定义列」调整，所以先按表头定位「日期」列下标。
    读不到时返回 None（此时跳过校验，避免误伤）。
    """
    return page.evaluate(r"""
    (() => {
      const visible = element => Boolean(
        element.offsetWidth || element.offsetHeight || element.getClientRects().length
      );
      const text = element => (element.innerText || element.textContent || "").trim();
      const headers = [...document.querySelectorAll(".el-table__header th")]
        .filter(visible).map(text);
      const index = headers.findIndex(name => name === "日期");
      if (index < 0) return null;
      const row = [...document.querySelectorAll(".el-table__body tr")].filter(visible)[0];
      if (!row) return null;
      const cells = [...row.querySelectorAll("td")];
      if (cells.length <= index) return null;
      const match = text(cells[index]).match(/\d{4}-\d{2}-\d{2}/);
      return match ? match[0] : null;
    })()
    """)


def reload_target_tab(page, timeout=40):
    """重载标签页，让筛选控件的内部模型回到页面默认值（昨天）。

    账号管家标签页是长期存活的浏览器上下文。Element 的日期范围控件把
    「显示值」和「内部模型」分开存：直接写 input.value 只改显示，查询仍会用
    上次加载页面时的旧日期。实测 2026-09-22 —— 输入框显示 09-21，而表格
    「日期」列整列都是 09-20，整批采错了一天的数据且毫无报错。
    重载后页面按自身默认初始化，显示与模型才一致。顺带清掉残留筛选。
    """
    print("重载内部系统标签页，重置筛选控件状态...")
    page.call("Page.reload", {"ignoreCache": False})
    time.sleep(2)
    wait_for_ready(page, timeout=timeout)
    wait_for_material_controls(page, timeout=timeout)


def verify_applied_date(page, expected_date):
    """核实查询真正生效的日期；不一致立即中止，绝不导出错误日期的数据。"""
    applied = read_applied_date(page)
    if not applied:
        print("  ⚠ 未读到表格「日期」列，跳过日期核实")
        return
    if applied != expected_date:
        raise CDPError(
            f"查询日期未生效：期望 {expected_date}，表格实际为 {applied}。已中止导出，"
            "避免采错整天的数据。请重试；若反复出现，请在账号管家里手动刷新该标签页。"
        )
    print(f"✓ 已核实查询日期生效：{applied}")


def current_page_state(page):
    return page.evaluate("""
    (() => ({
      url: location.href,
      title: document.title,
      readyState: document.readyState
    }))()
    """)


def wait_for_ready(page, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = current_page_state(page)
            if state and state.get("readyState") in {"interactive", "complete"}:
                return state
        except Exception:
            pass
        time.sleep(0.4)
    raise CDPError("内部系统页面加载超时")


def find_internal_target():
    response = requests.get(f"{CDP_ENDPOINT}/json/list", timeout=5)
    response.raise_for_status()
    candidates = [
        target
        for target in response.json()
        if target.get("type") == "page"
        and INTERNAL_HOST_SUFFIX in target.get("url", "")
        and target.get("webSocketDebuggerUrl")
    ]
    if not candidates:
        return None
    exact = [
        target for target in candidates
        if "video-material-consumption" in target.get("url", "")
    ]
    return (exact or candidates)[-1]


def inspect_internal_page(page):
    return page.evaluate("""
    (() => {
      const visible = element => Boolean(
        element.offsetWidth || element.offsetHeight || element.getClientRects().length
      );
      const count = selector => [...document.querySelectorAll(selector)]
        .filter(visible).length;
      const countText = (selector, text) => [...document.querySelectorAll(selector)]
        .filter(visible)
        .filter(element => (element.innerText || element.textContent || "").trim() === text)
        .length;
      return {
        url: location.href,
        title: document.title,
        startDate: count("input[placeholder='开始日期']"),
        materialType: count("input[placeholder='选择素材类型']"),
        queryButton: countText("button", "查询"),
        exportButton: countText("button", "导出数据"),
        loginPlatform: (document.body?.innerText || '').includes('选择登录平台')
      };
    })()
    """)


def wait_for_material_controls(page, timeout=15):
    """等待 SPA 明细控件真正就绪，而不只依赖 document.readyState。"""
    deadline = time.monotonic() + timeout
    last_info = None
    required = ("startDate", "materialType", "queryButton", "exportButton")
    while time.monotonic() < deadline:
        try:
            last_info = inspect_internal_page(page)
            if all(last_info.get(key, 0) > 0 for key in required):
                return last_info
            if last_info.get("loginPlatform"):
                raise CDPError("内部系统登录态失效，页面仍停留在登录平台选择页")
        except CDPError:
            raise
        except Exception:
            pass
        time.sleep(0.4)

    missing = [key for key in required if not (last_info or {}).get(key)]
    raise CDPError(
        "内部系统素材消耗页未就绪，缺少控件：" + ", ".join(missing)
    )


def wait_for_download(download_dir, previous_names, started_at, timeout=60):
    directory = Path(download_dir)
    deadline = time.monotonic() + timeout
    previous_sizes = {}

    while time.monotonic() < deadline:
        candidates = []
        for path in directory.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".xlsx", ".xls"}:
                continue
            stat = path.stat()
            if path.name not in previous_names or stat.st_mtime >= started_at:
                candidates.append((stat.st_mtime, path, stat.st_size))

        for _, path, size in sorted(candidates, reverse=True):
            if size > 0 and previous_sizes.get(path) == size:
                return str(path)
            previous_sizes[path] = size

        time.sleep(0.6)

    raise CDPError("等待内部系统导出文件超时")


def run_direct_export(
    download_dir,
    process_excel_callback=None,
    probe_only=False,
    filter_date="",
):
    # 先确保 9223 就绪：不通会自动拉起账号管家并等待
    try:
        from cdp_guard import ensure_cdp, get_cdp_endpoint
        if not ensure_cdp():
            print("✗ 9223 调试端口不可用，请开启账号管家调试端口后重试")
            return False
        global CDP_ENDPOINT
        CDP_ENDPOINT = get_cdp_endpoint()
    except ImportError:
        pass
    target = find_internal_target()
    if not target:
        print("✗ 9223 端口中未找到已打开的内部系统标签页")
        return False

    page = CDPClient(target["webSocketDebuggerUrl"])
    browser = None
    download_context_id = None
    download_behavior_changed = False
    try:
        if not BACKGROUND_MODE:
            page.call("Page.bringToFront")
        state = wait_for_ready(page)
        print(f"✓ 已直连内部系统标签页：{state['url']}")

        if "login#/login" in state["url"]:
            print("✗ 当前内部系统标签页处于登录页，请先在账号管家中完成登录")
            return False

        if "video-material-consumption" not in state["url"]:
            print("正在通过页面级 CDP 进入素材消耗统计明细页面...")
            page.call("Page.navigate", {"url": INTERNAL_URL})
            time.sleep(2)
            state = wait_for_ready(page)

        page_info = wait_for_material_controls(page)
        if probe_only:
            print(
                "直连探测通过："
                f"日期={page_info['startDate']}，"
                f"素材类型={page_info['materialType']}，"
                f"查询={page_info['queryButton']}，"
                f"导出={page_info['exportButton']}"
            )
            return True

        # 长期存活的标签页会保留上次加载时的日期模型，必须先重载：
        # 否则下面 set_filter_date 只改到「显示值」，查询仍用旧日期。
        reload_target_tab(page)

        if filter_date:
            print(f"选择原任务筛选日期：{filter_date}...")
            target_date = filter_date
        else:
            target_date = _yesterday_date_text()
            print(f"选择时间范围为“昨天”（{target_date}）...")
        set_filter_date(page, target_date)

        press_escape(page)
        time.sleep(0.4)

        print("选择“图片”创意类型...")
        if click_element(page, "input[placeholder='选择素材类型']"):
            time.sleep(0.8)
            if not click_element(
                page,
                "li.el-select-dropdown__item, [role='option']",
                "图片",
                exact=True,
            ):
                print("  ⚠ 未找到“图片”选项，沿用页面当前素材类型")
            time.sleep(0.8)
        else:
            print("  ⚠ 未找到素材类型选择框，沿用页面当前素材类型")

        press_escape(page)
        time.sleep(0.4)

        print("清理“产品”残留筛选...")
        clear_product_filter(page)
        time.sleep(0.4)

        print("点击查询...")
        if not click_element(page, "button", "查询", exact=True):
            raise CDPError("未找到“查询”按钮")
        time.sleep(5)

        total = read_result_total(page)
        if total == 0:
            raise CDPError(
                "内部系统查询结果为 0 条，已停止导出："
                "请检查账号管家页面上的时间范围、产品、素材类型筛选是否有残留条件"
            )
        if total is not None:
            print(f"✓ 查询结果共 {total} 条")

        # 导出前核实：查询真正生效的日期必须是目标日期，否则中止。
        verify_applied_date(page, target_date)

        print("正在全选数据...")
        selected = click_element(
            page,
            ".el-table__header-wrapper .el-checkbox, thead .el-checkbox",
        )
        if not selected:
            selected = click_element(page, ".el-checkbox")
        if not selected:
            raise CDPError("未找到列表全选框")
        time.sleep(1.5)

        Path(download_dir).mkdir(parents=True, exist_ok=True)
        previous_names = {
            path.name for path in Path(download_dir).iterdir() if path.is_file()
        }
        started_at = time.time()

        version = requests.get(f"{CDP_ENDPOINT}/json/version", timeout=5).json()
        browser = CDPClient(version["webSocketDebuggerUrl"])
        target_info = browser.call(
            "Target.getTargetInfo",
            {"targetId": target["id"]},
        ).get("targetInfo", {})
        download_context_id = target_info.get("browserContextId")
        download_params = {
            "behavior": "allow",
            "downloadPath": os.path.abspath(download_dir),
            "eventsEnabled": True,
        }
        if download_context_id:
            download_params["browserContextId"] = download_context_id
        try:
            browser.call(
                "Browser.setDownloadBehavior",
                download_params,
            )
        except CDPError as scoped_error:
            if not download_context_id:
                raise
            print(
                "  ⚠ 账号管家返回了不可识别的浏览器上下文，"
                "改用全局下载设置"
            )
            download_context_id = None
            download_params.pop("browserContextId", None)
            browser.call(
                "Browser.setDownloadBehavior",
                download_params,
            )
        download_behavior_changed = True

        print("点击导出按钮...")
        if not click_element(page, "button", "导出数据", exact=True):
            raise CDPError("未找到“导出数据”按钮")

        file_path = wait_for_download(
            download_dir,
            previous_names,
            started_at,
        )
        print(f"✓ 数据已导出：{file_path}")

        if process_excel_callback:
            process_excel_callback(file_path)
        return True
    except Exception as error:
        print(f"目标标签页直连导出失败：{error}")
        return False
    finally:
        page.close()
        if browser:
            if download_behavior_changed:
                try:
                    reset_params = {"behavior": "default"}
                    if download_context_id:
                        reset_params["browserContextId"] = download_context_id
                    browser.call("Browser.setDownloadBehavior", reset_params)
                except Exception:
                    pass
            browser.close()


def _yesterday_date_text():
    return (datetime.date.today() - datetime.timedelta(days=1)).isoformat()


def _build_video_probe_script(keyword, top_n, page_size, filter_date):
    """在 内部系统页面上下文里完成 applist 筛选 + 视频明细拉取。

    API 请求必须带页面会话 Cookie，所以放在页面里执行；返回纯 JSON。
    app_ids 用字符串数组才能过滤（对象数组会报错，app_id 字段不生效）。
    """
    keyword_json = json.dumps(keyword, ensure_ascii=False)
    return f"""
    (async () => {{
      const keyword = {keyword_json};
      const topN = {int(top_n)};
      const pageSize = {int(page_size)};
      const base = location.origin;
      const appsResp = await fetch(base + "/api/api/materialReport/applist", {{credentials: "include"}});
      const appsJson = await appsResp.json();
      const appsData = appsJson && appsJson.data ? appsJson.data : {{}};
      const apps = Array.isArray(appsData) ? appsData
        : Array.isArray(appsData.list) ? appsData.list
        : Array.isArray(appsJson) ? appsJson : [];
      let matched = [];
      if (keyword) {{
        matched = apps.filter(a => String(a.app_name || "").includes(keyword));
      }}
      const appIds = matched.map(a => String(a.app_id));
      const d = new Date(Date.now() - 86400000);
      const pad = n => String(n).padStart(2, "0");
      const yesterday = `${{d.getFullYear()}}-${{pad(d.getMonth() + 1)}}-${{pad(d.getDate())}}`;
      const date = {json.dumps(filter_date, ensure_ascii=False)} || yesterday;
      const body = {{
        page: 1, page_size: pageSize, app_ids: appIds, media: "",
        creative_file_name: "", has_edit_log: "", image_name: "",
        start_date: date, end_date: date, app_id: [], media_id: [],
        department: "", group: "", user_name: "后期-示例用户"
      }};
      const detailResp = await fetch(base + "/api/api/materialReport/getVideoConsumptionDetail", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        credentials: "include",
        body: JSON.stringify(body)
      }});
      if (!detailResp.ok) throw new Error("视频明细接口 HTTP " + detailResp.status);
      const detail = await detailResp.json();
      if (!detail || detail.code !== 200) {{
        throw new Error("视频明细接口返回异常：" + JSON.stringify(detail && detail.message || detail));
      }}
      const data = (detail && detail.data) || {{}};
      const list = Array.isArray(data.list) ? data.list : [];
      return {{
        keyword, date, topN, pageSize,
        app_total: apps.length, matched_apps: matched.length,
        count: data.count || 0,
        total_consumption: data.total_consumption || 0,
        records: list.slice(0, topN)
      }};
    }})()
    """


def _video_file_extension(record):
    """优先从 creative_file_name 取扩展名，兜底 mp4。"""
    for field in ("creative_file_name", "video_name"):
        value = str(record.get(field) or "").strip()
        if "." in value:
            ext = value.rsplit(".", 1)[-1].split("?")[0].lower()
            if ext and ext.isalnum() and len(ext) <= 5:
                return ext
    return "mp4"


def _download_video_files(payload, save_dir):
    """用 python requests 下载 top N 视频（页面 fetch 会被 CORS 拦截）。

    返回 (capture_items, media_mapping)；文件名沿用 消耗-名称.扩展名 约定。
    """
    import requests as requests_module
    import re as re_module

    records = payload.get("records") or []
    save_path = Path(save_dir)
    save_path.mkdir(parents=True, exist_ok=True)
    session = requests_module.Session()
    session.trust_env = False

    capture_items = []
    media_mapping = {}
    success = 0
    failed = 0
    total = len(records)
    for position, record in enumerate(records, start=1):
        url = str(record.get("cover_url") or "").strip()
        name = str(record.get("video_name") or record.get("creative_file_name") or "").strip()
        consumption = record.get("sum_cost") or 0
        app_name = str(record.get("app_name") or "").strip()
        media = str(record.get("media") or "").strip()
        if not url or not name:
            print(f"[{position}/{total}] 跳过无效数据")
            continue
        ext = _video_file_extension(record)
        safe_name = re_module.sub(r'[\\/:*?"<>|]', '_', name).strip() or "未命名素材"
        try:
            consumption_text = f"{float(consumption):.2f}"
        except (TypeError, ValueError):
            consumption_text = "未知"
        filename = f"{consumption_text}-{safe_name}.{ext}"
        filepath = save_path / filename
        mapping_value = {"media": media, "channel": media}
        if filepath.exists() and filepath.stat().st_size > 0:
            media_mapping.setdefault(filename, mapping_value)
            capture_items.append({"file_name": filename, "source_url": url,
                "source_content_id": str(record.get("material_id") or name), "title": name,
                "content_text": f"消费 {consumption_text}",
                "raw": {"media": media, "channel": media, "app_name": app_name,
                        "data_platform": media, "consumption": consumption,
                        "material_type": "视频"}})
            print(f"[{position}/{total}] 跳过已存在: {filename}")
            continue
        try:
            response = session.get(url, timeout=60)
            if response.status_code == 200 and response.content:
                filepath.write_bytes(response.content)
                media_mapping[filename] = mapping_value
                capture_items.append({"file_name": filename, "source_url": url,
                    "source_content_id": str(record.get("material_id") or name), "title": name,
                    "content_text": f"消费 {consumption_text}",
                    "raw": {"media": media, "channel": media, "app_name": app_name,
                            "data_platform": media, "consumption": consumption,
                            "material_type": "视频"}})
                success += 1
                print(f"[{position}/{total}] ✓ 下载成功: {filename} (消耗:{consumption_text})")
            else:
                failed += 1
                print(f"[{position}/{total}] ✗ 下载失败: {name} (HTTP {response.status_code})")
        except Exception as error:
            failed += 1
            print(f"[{position}/{total}] ✗ 下载失败: {name} ({error})")

    print(f"视频下载完成！成功: {success}，失败: {failed}")
    return capture_items, media_mapping


def run_direct_video_export(
    save_dir,
    report_dir,
    capture_items_writer,
    keyword="保险",
    top_n=50,
    filter_date="",
):
    """API 直采模式：产品关键词过滤 + 视频消耗 Top N。

    不需要页面 Excel 导出；在页面上下文调用 JSON 接口，python 下载 mp4。
    capture_items_writer(capture_items) 负责写 capture-items.json。
    """
    try:
        from cdp_guard import ensure_cdp, get_cdp_endpoint
        if not ensure_cdp():
            print("✗ 9223 调试端口不可用，请开启账号管家调试端口后重试")
            return False
        global CDP_ENDPOINT
        CDP_ENDPOINT = get_cdp_endpoint()
    except ImportError:
        pass
    target = find_internal_target()
    if not target:
        print("✗ 9223 端口中未找到已打开的内部系统标签页")
        return False

    page = CDPClient(target["webSocketDebuggerUrl"])
    try:
        state = wait_for_ready(page)
        print(f"✓ 已直连内部系统标签页：{state['url']}")
        if "login#/login" in state["url"]:
            print("✗ 当前内部系统标签页处于登录页，请先在账号管家中完成登录")
            return False

        effective_date = filter_date or _yesterday_date_text()
        page_size = max(int(top_n), 1)
        print(f"API 直采视频：关键词=「{keyword}」，Top N={top_n}，日期={effective_date}")
        payload = page.evaluate(
            _build_video_probe_script(keyword, top_n, page_size, effective_date),
            timeout=120,
        )
        if not isinstance(payload, dict) or "error" in payload:
            raise CDPError(f"视频明细接口调用失败：{(payload or {}).get('error') if isinstance(payload, dict) else payload}")

        print(
            f"✓ 产品总数 {payload.get('app_total')}，匹配「{keyword}」{payload.get('matched_apps')} 个，"
            f"视频总数 {payload.get('count')}，本次取 Top {min(len(payload.get('records') or []), int(top_n))}"
        )
        records = payload.get("records") or []
        if not records:
            print("✗ 没有匹配的视频数据，请检查关键词或日期")
            return False

        capture_items, media_mapping = _download_video_files(payload, save_dir)
        if capture_items_writer:
            capture_items_writer(capture_items)
        mapping_path = Path(report_dir) / "media-map.json"
        try:
            mapping_path.parent.mkdir(parents=True, exist_ok=True)
            # 同一天的多次采集共用一个日期目录，映射表要合并而不是覆盖，
            # 否则后一次会把前一次的渠道映射冲掉，上游系统导入时那批图会掉进「未识别渠道」。
            merged_mapping = {}
            if mapping_path.is_file():
                try:
                    existing_mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
                    if isinstance(existing_mapping, dict):
                        merged_mapping.update(existing_mapping)
                except (OSError, ValueError):
                    pass
            merged_mapping.update(media_mapping)
            with open(mapping_path, "w", encoding="utf-8") as mapping_file:
                json.dump(merged_mapping, mapping_file, ensure_ascii=False, indent=2)
            print(f"媒体/渠道字段映射已保存（累计 {len(merged_mapping)} 项）: {mapping_path}")
        except Exception as error:
            print(f"保存媒体/渠道字段映射失败: {error}")

        summary_path = Path(report_dir) / "video-api-summary.json"
        try:
            with open(summary_path, "w", encoding="utf-8") as summary_file:
                json.dump({
                    "keyword": payload.get("keyword"),
                    "date": payload.get("date"),
                    "app_total": payload.get("app_total"),
                    "matched_apps": payload.get("matched_apps"),
                    "video_count": payload.get("count"),
                    "total_consumption": payload.get("total_consumption"),
                    "downloaded": len(capture_items),
                }, summary_file, ensure_ascii=False, indent=2)
        except Exception as error:
            print(f"保存视频采集摘要失败: {error}")
        return True
    except Exception as error:
        print(f"API 直采视频失败：{error}")
        return False
    finally:
        page.close()


def main():
    from output_layout import get_batch_layout

    parser = argparse.ArgumentParser(description="内部系统目标标签页直连导出")
    parser.add_argument("--probe", action="store_true", help="仅检测页面，不执行导出")
    parser.add_argument(
        "--download-dir",
        default=os.environ.get(
            "MATERIAL_DOWNLOADER_EXPORT_DIR",
            r"D:\跑量素材\外部跑量素材",
        ),
    )
    parser.add_argument(
        "--filter-date",
        default=os.environ.get("MATERIAL_DOWNLOADER_FILTER_DATE", ""),
        help="固定查询日期（YYYY-MM-DD）；用于按原任务日期重试",
    )
    parser.add_argument("--video-api", action="store_true", help="使用 API 直采视频模式")
    parser.add_argument("--keyword", default=os.environ.get("MATERIAL_DOWNLOADER_INTERNAL_KEYWORD", "保险"))
    parser.add_argument("--top-n", type=int, default=int(os.environ.get("MATERIAL_DOWNLOADER_INTERNAL_TOP_N", "50") or 50))
    args = parser.parse_args()
    layout = get_batch_layout("内部系统", args.download_dir)
    if args.video_api:
        success = run_direct_video_export(
            str(layout["raw_dir"]),
            str(layout["report_dir"]),
            lambda items: __import__("output_layout").write_capture_items(layout, items),
            keyword=args.keyword,
            top_n=args.top_n,
            filter_date=args.filter_date.strip(),
        )
        raise SystemExit(0 if success else 1)
    download_dir = str(layout["report_dir"])
    success = run_direct_export(
        download_dir,
        probe_only=args.probe,
        filter_date=args.filter_date.strip(),
    )
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()

