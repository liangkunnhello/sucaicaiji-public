#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""页面级 CDP 轻量适配层。

用于浏览器级 Playwright ``connect_over_cdp`` 已连上 WebSocket、却在初始化
所有标签页/扩展目标时卡死的场景。这里只连接指定页面的 DevTools WebSocket，
不会启动、关闭或重置用户浏览器，也不会触碰登录数据。
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from urllib.parse import quote, urlparse
from dataclasses import dataclass
from itertools import count
from typing import Any

from websockets.sync.client import connect


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


class DirectCDPError(RuntimeError):
    pass


class CDPConnection:
    def __init__(
        self,
        websocket_url: str,
        timeout: float = 10.0,
        enable_page_domains: bool = True,
    ):
        self.websocket_url = websocket_url
        self.timeout = timeout
        self._ids = count(1)
        self._socket = connect(
            websocket_url,
            open_timeout=timeout,
            close_timeout=2,
            max_size=None,
        )
        if enable_page_domains:
            self.call("Runtime.enable")
            self.call("Page.enable")

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = next(self._ids)
        self._socket.send(json.dumps({
            "id": request_id,
            "method": method,
            "params": params or {},
        }))
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DirectCDPError(f"CDP调用超时：{method}")
            message = json.loads(self._socket.recv(timeout=remaining))
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                raise DirectCDPError(
                    f"CDP {method} 失败：{error.get('message', error)}"
                )
            return message.get("result", {})

    def evaluate(self, expression: str, context_id: int | None = None) -> Any:
        params: dict[str, Any] = {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
            "userGesture": True,
        }
        if context_id is not None:
            params["contextId"] = int(context_id)
        result = self.call("Runtime.evaluate", params)
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            exception = details.get("exception", {})
            raise DirectCDPError(
                "页面脚本执行失败："
                f"{exception.get('description') or details.get('text', '未知错误')}"
            )
        remote = result.get("result", {})
        if remote.get("subtype") == "error":
            raise DirectCDPError(remote.get("description", "页面脚本执行失败"))
        return remote.get("value")

    def close(self) -> None:
        try:
            self._socket.close()
        except Exception:
            pass


def _selector_parts(selector: str) -> tuple[str, str | None, str | None]:
    """把少量 Playwright 文本伪选择器转换为浏览器可执行的查询参数。"""
    has_text = re.fullmatch(r"(.+):has-text\('([^']*)'\)", selector)
    if has_text:
        return has_text.group(1), has_text.group(2), None

    has_child_text = re.fullmatch(
        r"(.+):has\((.+):text\('([^']*)'\)\)",
        selector,
    )
    if has_child_text:
        child_selector = has_child_text.group(2)
        child_text = has_child_text.group(3)
        return has_child_text.group(1), None, f"{child_selector}\0{child_text}"
    return selector, None, None


class QueryRoot:
    page: "DirectPage"

    def _root_expression(self) -> str:
        raise NotImplementedError

    def _collection_expression(self, selector: str) -> str:
        css, own_text, child_text = _selector_parts(selector)
        root = self._root_expression()
        expression = (
            f"Array.from(({root}).querySelectorAll({_json(css)}))"
        )
        if own_text is not None:
            expression += (
                f".filter(el => (el.innerText || el.textContent || '').includes({_json(own_text)}))"
            )
        if child_text is not None:
            child_selector, text = child_text.split("\0", 1)
            expression += (
                ".filter(el => {"
                f"const child = el.querySelector({_json(child_selector)});"
                f"return child && (child.innerText || child.textContent || '').includes({_json(text)});"
                "})"
            )
        return expression

    def query_selector(self, selector: str) -> "DirectElement | None":
        collection = self._collection_expression(selector)
        if int(self.page._evaluate(f"({collection}).length") or 0) < 1:
            return None
        return DirectElement(self.page, collection, 0)

    def query_selector_all(self, selector: str) -> list["DirectElement"]:
        collection = self._collection_expression(selector)
        length = int(self.page._evaluate(f"({collection}).length") or 0)
        return [DirectElement(self.page, collection, index) for index in range(length)]

    def locator(self, selector: str) -> "DirectLocator":
        """Return the small locator subset used by the collectors.

        The collector scripts were originally written for Playwright.  Keeping
        this adapter intentionally small lets them use a page-level CDP socket
        without asking Chromium to enumerate every extension and tab.
        """
        return DirectLocator(self, selector)

    def get_by_text(self, text: str, exact: bool = False) -> "DirectTextLocator":
        return DirectTextLocator(self, text, exact=exact)


class DirectKeyboard:
    KEY_DATA = {
        "Escape": ("Escape", "Escape", 27),
        "Enter": ("Enter", "Enter", 13),
    }

    def __init__(self, page: "DirectPage"):
        self.page = page

    def press(self, key_name: str) -> None:
        raw_name = str(key_name or "")
        parts = raw_name.split("+")
        key_name = parts[-1] or raw_name
        modifiers = 0
        for modifier in parts[:-1]:
            modifiers |= {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}.get(modifier, 0)
        key, code, key_code = self.KEY_DATA.get(
            key_name,
            (key_name, key_name, ord(key_name[0]) if key_name else 0),
        )
        params = {
            "key": key,
            "code": code,
            "windowsVirtualKeyCode": key_code,
            "nativeVirtualKeyCode": key_code,
            "modifiers": modifiers,
        }
        self.page.connection.call("Input.dispatchKeyEvent", {
            **params,
            "type": "keyDown",
        })
        self.page.connection.call("Input.dispatchKeyEvent", {
            **params,
            "type": "keyUp",
        })


class DirectMouse:
    def __init__(self, page: "DirectPage"):
        self.page = page
        self.x = 0.0
        self.y = 0.0

    def move(self, x: float, y: float) -> None:
        self.x, self.y = float(x), float(y)
        self.page.connection.call("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": self.x, "y": self.y,
        })

    def down(self, button: str = "left", click_count: int = 1) -> None:
        self.page.connection.call("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": self.x, "y": self.y,
            "button": button, "clickCount": click_count,
        })

    def up(self, button: str = "left", click_count: int = 1) -> None:
        self.page.connection.call("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": self.x, "y": self.y,
            "button": button, "clickCount": click_count,
        })

    def click(self, x: float, y: float, button: str = "left", click_count: int = 1) -> None:
        self.move(x, y)
        params = {
            "x": float(x), "y": float(y), "button": button,
            "clickCount": click_count,
        }
        self.page.connection.call("Input.dispatchMouseEvent", {
            "type": "mousePressed", **params,
        })
        self.page.connection.call("Input.dispatchMouseEvent", {
            "type": "mouseReleased", **params,
        })


class DirectPage(QueryRoot):
    def __init__(self, connection: CDPConnection, target: dict[str, Any], context: "DirectContext | None" = None):
        self.connection = connection
        self.target = target
        self.page = self
        self.context = context
        self.keyboard = DirectKeyboard(self)
        self.mouse = DirectMouse(self)

    @property
    def frames(self) -> list["DirectPage"]:
        frames: list[DirectPage] = [self]
        try:
            tree = self.connection.call("Page.getFrameTree").get("frameTree", {})
            children = tree.get("childFrames", [])
            while children:
                child = children.pop(0)
                frame_info = child.get("frame", {})
                frame_id = frame_info.get("id")
                if frame_id and frame_id != self.target.get("frameId"):
                    try:
                        isolated = self.connection.call("Page.createIsolatedWorld", {
                            "frameId": frame_id,
                            "worldName": "sibling-direct-cdp",
                            "grantUniveralAccess": True,
                        })
                        frames.append(DirectFrame(
                            self, frame_id, isolated.get("executionContextId")
                        ))
                    except Exception:
                        pass
                children.extend(child.get("childFrames", []))
        except Exception:
            pass
        return frames

    def _root_expression(self) -> str:
        return "document"

    def _evaluate(self, expression: str) -> Any:
        return self.connection.evaluate(expression)

    @property
    def url(self) -> str:
        return str(self._evaluate("location.href") or self.target.get("url", ""))

    def evaluate(self, script: str, *args: Any) -> Any:
        """Evaluate a Playwright-style script with optional positional args."""
        return self._evaluate_script(script, args)

    def _evaluate_script(self, script: str, args: tuple[Any, ...] = ()) -> Any:
        source = str(script or "").strip()
        encoded_args = ", ".join(_json(value) for value in args)
        if re.match(r"^(async\s*)?\(?[\w\s,]*\)?\s*=>", source):
            expression = f"({source})({encoded_args})"
        else:
            expression = f"(() => {{ {source} }})()"
        return self._evaluate(expression)

    def goto(self, url: str, wait_until: str = "load", timeout: int = 30000, **_kwargs) -> None:
        result = self.connection.call("Page.navigate", {"url": str(url)})
        if result.get("errorText"):
            raise DirectCDPError(f"页面导航失败：{result['errorText']}")
        self.wait_for_load_state(wait_until, timeout=timeout)

    def reload(self, wait_until: str = "load", timeout: int = 30000, **_kwargs) -> None:
        self.connection.call("Page.reload", {"ignoreCache": False})
        self.wait_for_load_state(wait_until, timeout=timeout)

    def wait_for_timeout(self, milliseconds: float) -> None:
        time.sleep(max(0, float(milliseconds)) / 1000)

    def bring_to_front(self) -> None:
        self.connection.call("Page.bringToFront")

    def close(self) -> None:
        try:
            self.connection.call("Page.close")
        except Exception:
            pass
        finally:
            self.connection.close()

    def wait_for_load_state(self, _state: str = "load", timeout: int = 30000) -> None:
        deadline = time.monotonic() + timeout / 1000
        complete_since: float | None = None
        while time.monotonic() < deadline:
            try:
                ready = self._evaluate("document.readyState")
            except Exception:
                # Navigation briefly destroys the old execution context.  Keep
                # polling until the new document is available instead of
                # turning that normal transition into a false task failure.
                complete_since = None
                time.sleep(0.1)
                continue
            if ready == "complete" or (_state == "domcontentloaded" and ready == "interactive"):
                complete_since = complete_since or time.monotonic()
                if time.monotonic() - complete_since >= 0.4:
                    return
            else:
                complete_since = None
            time.sleep(0.1)
        raise DirectCDPError(f"页面加载等待超时：{timeout}ms")


class DirectElement(QueryRoot):
    def __init__(self, page: DirectPage, collection_expression: str, index: int):
        self.page = page
        self.collection_expression = collection_expression
        self.index = index

    def _root_expression(self) -> str:
        return f"(({self.collection_expression})[{self.index}])"

    def evaluate(self, script: str, *args: Any) -> Any:
        encoded_args = ", ".join(_json(value) for value in args)
        suffix = f", {encoded_args}" if encoded_args else ""
        return self.page._evaluate(f"({script})({self._root_expression()}{suffix})")

    def inner_text(self) -> str:
        return str(self.page._evaluate(
            f"((el) => el ? (el.innerText || el.textContent || '') : '')"
            f"({self._root_expression()})"
        ) or "")

    def get_attribute(self, name: str) -> str | None:
        return self.page._evaluate(
            f"((el) => el ? el.getAttribute({_json(name)}) : null)"
            f"({self._root_expression()})"
        )

    def click(self, timeout: int | None = None, **_kwargs) -> None:
        self.page._evaluate(
            "((el) => {"
            "if (!el) throw new Error('元素不存在');"
            "el.scrollIntoView({block:'center',inline:'center'});"
            "el.focus({preventScroll:true});"
            "el.click();"
            "return true;"
            f"}})({self._root_expression()})"
        )

    def fill(self, value: str) -> None:
        self.page._evaluate(
            "((el, value) => {"
            "if (!el) throw new Error('输入框不存在');"
            "const setter = Object.getOwnPropertyDescriptor("
            "Object.getPrototypeOf(el), 'value')?.set;"
            "if (setter) setter.call(el, value); else el.value = value;"
            "el.dispatchEvent(new Event('input', {bubbles:true}));"
            "el.dispatchEvent(new Event('change', {bubbles:true}));"
            "})"
            f"({self._root_expression()}, {_json(value)})"
        )

    def press(self, key_name: str) -> None:
        self.page._evaluate(
            f"((el) => {{ if (el) el.focus(); }})({self._root_expression()})"
        )
        self.page.keyboard.press(key_name)

    def is_visible(self) -> bool:
        return bool(self.page._evaluate(
            f"((el) => Boolean(el && el.offsetParent !== null && "
            f"el.getBoundingClientRect().width && el.getBoundingClientRect().height))"
            f"({self._root_expression()})"
        ))

    def input_value(self) -> str:
        return str(self.page._evaluate(
            f"((el) => el && 'value' in el ? el.value : '')({self._root_expression()})"
        ) or "")


class DirectFrame(QueryRoot):
    """A frame execution context backed by the same page-level CDP socket."""

    def __init__(self, parent: DirectPage, frame_id: str, context_id: int | None):
        self.parent = parent
        self.connection = parent.connection
        self.frame_id = frame_id
        self.context_id = context_id
        self.page = self
        self.keyboard = DirectKeyboard(self)
        self.mouse = DirectMouse(self)

    def _root_expression(self) -> str:
        return "document"

    def _evaluate(self, expression: str) -> Any:
        return self.connection.evaluate(expression, context_id=self.context_id)

    def evaluate(self, script: str, *args: Any) -> Any:
        source = str(script or "").strip()
        encoded_args = ", ".join(_json(value) for value in args)
        if re.match(r"^(async\s*)?\(?[\w\s,]*\)?\s*=>", source):
            expression = f"({source})({encoded_args})"
        else:
            expression = f"(() => {{ {source} }})()"
        return self._evaluate(expression)

    @property
    def url(self) -> str:
        return self.parent.url


class DirectLocator(QueryRoot):
    def __init__(self, root: QueryRoot, selector: str):
        self.page = root.page
        self.root = root
        self.selector = selector

    def _root_expression(self) -> str:
        return self.root._root_expression()

    def count(self) -> int:
        return int(self.page._evaluate(f"({self._collection_expression(self.selector)}).length") or 0)

    def nth(self, index: int) -> DirectElement:
        return DirectElement(self.page, self._collection_expression(self.selector), int(index))

    def is_visible(self) -> bool:
        return self.count() > 0 and self.nth(0).is_visible()

    def click(self, timeout: int | None = None, **_kwargs) -> None:
        deadline = time.monotonic() + (max(0, timeout) / 1000 if timeout else 0)
        while True:
            if self.count() > 0:
                candidate = self.nth(0)
                if candidate.is_visible() or not timeout:
                    candidate.click()
                    return
            if not timeout or time.monotonic() >= deadline:
                raise DirectCDPError(f"元素不可点击：{self.selector}")
            time.sleep(0.05)

    def fill(self, value: str, **_kwargs) -> None:
        self.nth(0).fill(value)

    def press(self, key_name: str, **_kwargs) -> None:
        self.nth(0).press(key_name)

    def input_value(self) -> str:
        return self.nth(0).input_value()


class DirectTextLocator(DirectLocator):
    def __init__(self, root: QueryRoot, text: str, exact: bool = False):
        super().__init__(root, "*")
        self.text = str(text)
        self.exact = exact

    def _collection_expression(self, _selector: str) -> str:
        root = self._root_expression()
        text = _json(self.text)
        if self.exact:
            predicate = f"(el.innerText || el.textContent || '').trim() === {text}"
        else:
            predicate = f"(el.innerText || el.textContent || '').includes({text})"
        return f"Array.from(({root}).querySelectorAll('*')).filter(el => {predicate})"


class DirectContext:
    def __init__(self, browser: "DirectBrowser", page: DirectPage):
        self.browser = browser
        self.endpoint = browser.endpoint
        self._pages: dict[str, DirectPage] = {}
        self._url_hints = self._derive_url_hints(page.target.get("url", ""))
        self._register(page)

    @staticmethod
    def _derive_url_hints(raw_url: str) -> set[str]:
        parsed = urlparse(str(raw_url or ""))
        host = (parsed.hostname or "").lower()
        hints = {host} if host else set()
        parts = host.split(".")
        if len(parts) >= 2:
            hints.add(".".join(parts[-2:]))
        return hints

    def _is_related_target(self, target: dict[str, Any]) -> bool:
        raw_url = str(target.get("url", ""))
        if raw_url in {"", "about:blank"}:
            return True
        normalized = raw_url.lower()
        return any(hint and hint in normalized for hint in self._url_hints)

    def _register(self, page: DirectPage) -> DirectPage:
        page.context = self
        target_id = str(page.target.get("id", id(page)))
        self._pages[target_id] = page
        return page

    @property
    def pages(self) -> list[DirectPage]:
        if self.endpoint:
            try:
                # A newly created tab starts at about:blank and is navigated
                # later.  Refresh the hints from its current URL so a QQ/SSO
                # popup opened after that navigation is still discovered.
                for known_page in tuple(self._pages.values()):
                    try:
                        self._url_hints.update(self._derive_url_hints(known_page.url))
                    except Exception:
                        pass
                known = set(self._pages)
                for target in list_targets(self.endpoint, timeout=2.0):
                    target_id = str(target.get("id", ""))
                    if target_id in known or target.get("type") != "page" or not self._is_related_target(target):
                        continue
                    websocket_url = target.get("webSocketDebuggerUrl")
                    if websocket_url:
                        try:
                            self._register(DirectPage(
                                CDPConnection(websocket_url, timeout=5.0), target
                            ))
                        except Exception:
                            pass
            except Exception:
                pass
        return list(self._pages.values())

    def cookies(self) -> list[dict[str, Any]]:
        for page in self.pages:
            try:
                cookies = page.connection.call("Network.getAllCookies").get("cookies", [])
                if cookies:
                    return cookies
            except Exception:
                pass
            try:
                raw = str(page.evaluate("() => document.cookie") or "")
                host = urlparse(page.url).hostname or ""
                parsed = []
                for item in raw.split(";"):
                    if "=" not in item:
                        continue
                    name, value = item.strip().split("=", 1)
                    if name:
                        parsed.append({"name": name, "value": value, "domain": host, "path": "/"})
                if parsed:
                    return parsed
            except Exception:
                pass
        return []

    def new_page(self, url: str = "about:blank") -> DirectPage:
        source = self.pages[0] if self.pages else None
        if source is None:
            raise DirectCDPError("没有可用于创建新标签页的 CDP 页面")
        initial_ids = set(self._pages)
        try:
            source.evaluate(f"() => Boolean(window.open({_json(url)}, '_blank'))")
        except Exception:
            pass
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                for target in list_targets(self.endpoint, timeout=2.0):
                    target_id = str(target.get("id", ""))
                    if target_id in initial_ids or target.get("type") != "page":
                        continue
                    target_url = str(target.get("url", ""))
                    if target_url == "about:blank" or str(url) in target_url or "#" not in str(url):
                        websocket_url = target.get("webSocketDebuggerUrl")
                        if websocket_url:
                            page = DirectPage(CDPConnection(websocket_url, timeout=10.0), target)
                            self._register(page)
                            return page
            except Exception:
                pass
            time.sleep(0.1)
        if self.endpoint:
            from_target = open_target_in_context(
                self.endpoint, url, source.target.get("id", ""), timeout=5.0
            )
            page = DirectPage(CDPConnection(from_target["webSocketDebuggerUrl"], timeout=10.0), from_target)
            self._register(page)
            return page
        raise DirectCDPError(f"创建新标签页超时：{url}")


class DirectBrowser:
    def __init__(self, page: DirectPage, endpoint: str | None = None):
        self._page = page
        self.endpoint = endpoint
        self.contexts = [DirectContext(self, page)]
        self.is_direct_cdp = True
        page.context = self.contexts[0]

    def close(self) -> None:
        # 只断开本脚本的页面 WebSocket，不关闭用户浏览器或标签页。
        for page in self.contexts[0].pages:
            page.connection.close()


def list_targets(endpoint: str, timeout: float = 5.0) -> list[dict[str, Any]]:
    with urllib.request.urlopen(
        f"{endpoint.rstrip('/')}/json/list",
        timeout=timeout,
    ) as response:
        return json.load(response)


def open_target(endpoint: str, url: str, timeout: float = 5.0) -> dict[str, Any]:
    """Open a URL as a new tab in the already-running account-manager browser.

    OrayOnion exposes the standard Chrome DevTools ``/json/new`` endpoint, but
    recent Chromium versions require PUT while older builds accepted GET.  Try
    PUT first and retain a GET fallback for the older account-manager runtime.
    """
    encoded = quote(url, safe="")
    endpoint = endpoint.rstrip("/")
    last_error: Exception | None = None
    for method in ("PUT", "GET"):
        try:
            request = urllib.request.Request(
                f"{endpoint}/json/new?{encoded}",
                method=method,
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
            if isinstance(payload, dict):
                return payload
        except Exception as error:
            last_error = error
    raise DirectCDPError(f"无法在账号管家中打开页面 {url!r}: {last_error}")


def open_target_in_context(
    endpoint: str,
    url: str,
    source_target_id: str,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Open a tab in the same isolated browser context as a logged-in source tab."""
    with urllib.request.urlopen(
        f"{endpoint.rstrip('/')}/json/version",
        timeout=min(timeout, 5.0),
    ) as response:
        browser_websocket_url = json.load(response).get("webSocketDebuggerUrl")
    if not browser_websocket_url:
        raise DirectCDPError("账号管家未返回浏览器级 WebSocket")

    connection = CDPConnection(
        browser_websocket_url,
        timeout=timeout,
        enable_page_domains=False,
    )
    try:
        target_info = connection.call(
            "Target.getTargetInfo",
            {"targetId": str(source_target_id)},
        ).get("targetInfo", {})
        params = {"url": url}
        browser_context_id = target_info.get("browserContextId")
        if browser_context_id:
            params["browserContextId"] = browser_context_id
        created = connection.call("Target.createTarget", params)
        target_id = str(created.get("targetId", ""))
    finally:
        connection.close()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        target = next(
            (
                item for item in list_targets(endpoint, timeout=min(timeout, 5.0))
                if str(item.get("id", "")) == target_id
                and item.get("webSocketDebuggerUrl")
            ),
            None,
        )
        if target:
            return target
        time.sleep(0.1)
    raise DirectCDPError(f"同上下文任务标签创建超时：{target_id}")


def close_target(endpoint: str, target_id: str, timeout: float = 5.0) -> bool:
    """Close one known task-owned tab without touching the browser process."""
    target = next(
        (
            item for item in list_targets(endpoint, timeout=timeout)
            if str(item.get("id", "")) == str(target_id)
        ),
        None,
    )
    if not target:
        return False
    websocket_url = target.get("webSocketDebuggerUrl")
    if not websocket_url:
        return False
    connection = CDPConnection(websocket_url, timeout=timeout)
    try:
        try:
            connection.call("Page.close")
        except Exception:
            # Chromium can close the socket before returning the Page.close reply.
            pass
    finally:
        connection.close()
    return True


def connect_target_page(
    endpoint: str,
    url_keyword: str,
    timeout: float = 10.0,
    open_url: str | None = None,
) -> DirectBrowser:
    targets = list_targets(endpoint, timeout=min(timeout, 5.0))
    target = next(
        (
            item for item in targets
            if item.get("type") == "page"
            and url_keyword.lower() in str(item.get("url", "")).lower()
            and item.get("webSocketDebuggerUrl")
        ),
        None,
    )
    if not target and open_url:
        open_target(endpoint, open_url, timeout=min(timeout, 5.0))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            targets = list_targets(endpoint, timeout=min(timeout, 5.0))
            target = next(
                (
                    item for item in targets
                    if item.get("type") == "page"
                    and url_keyword.lower() in str(item.get("url", "")).lower()
                    and item.get("webSocketDebuggerUrl")
                ),
                None,
            )
            if target:
                break
            time.sleep(0.25)
    if not target:
        raise DirectCDPError(f"未找到 URL 包含 {url_keyword!r} 的页面")
    connection = CDPConnection(target["webSocketDebuggerUrl"], timeout=timeout)
    return DirectBrowser(DirectPage(connection, target), endpoint=endpoint)
