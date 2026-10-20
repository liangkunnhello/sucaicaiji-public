#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Open a platform through the persistent Junbo account-manager browser."""
from __future__ import annotations

import time

from direct_cdp import CDPConnection, DirectPage, list_targets, open_target


def _visible(element):
    try:
        return bool(element.evaluate(
            "el => !!(el && el.getBoundingClientRect().width && el.getBoundingClientRect().height)"
        ))
    except Exception:
        return False


def trusted_click(page, element):
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
        return True
    except Exception:
        return False


def manager_account_action(page, account_hint=""):
    try:
        rows = page.query_selector_all("tbody tr")
        if account_hint:
            for row in rows:
                if account_hint in row.inner_text():
                    action = row.query_selector(".action-btn")
                    if action:
                        return action
        for row in rows:
            action = row.query_selector(".action-btn")
            if action:
                return action
    except Exception:
        pass
    return None


def _platform_selected(platform):
    try:
        return bool(platform.evaluate(
            "el => Boolean(el.closest('.platform-item')?.classList.contains('selected'))"
        ))
    except Exception:
        return False


def _account_rows_signature(page):
    try:
        return tuple(row.inner_text() for row in page.query_selector_all("tbody tr"))
    except Exception:
        return ()


def _manager_target(endpoint, timeout):
    target = next(
        (
            item for item in list_targets(endpoint)
            if item.get("type") == "page"
            and item.get("url") == "chrome://newtab/"
            and item.get("webSocketDebuggerUrl")
        ),
        None,
    )
    if target:
        return target
    target = open_target(endpoint, "chrome://newtab/", timeout=min(timeout, 5))
    time.sleep(0.8)
    return target


def open_platform_application(
    endpoint,
    platform_name,
    account_hint,
    url_keywords,
    timeout=30,
):
    """Click account manager -> platform -> account -> open application."""
    targets = list_targets(endpoint)
    existing = {item.get("id"): str(item.get("url", "")) for item in targets}
    manager_target = _manager_target(endpoint, timeout)
    connection = CDPConnection(manager_target["webSocketDebuggerUrl"], timeout=15)
    page = DirectPage(connection, manager_target)
    try:
        platform = next(
            (
                item for item in page.query_selector_all(".platform-name")
                if platform_name in item.inner_text()
            ),
            None,
        )
        was_selected = bool(platform and _platform_selected(platform))
        previous_rows = _account_rows_signature(page)
        if not platform or not trusted_click(page, platform):
            return None


        deadline = time.time() + 8
        while time.time() < deadline and not _platform_selected(platform):
            time.sleep(0.1)
        if not _platform_selected(platform):
            return None

        if not was_selected:
            while time.time() < deadline:
                current_rows = _account_rows_signature(page)
                if current_rows and current_rows != previous_rows:
                    break
                time.sleep(0.1)

        action = None
        deadline = time.time() + 8
        while time.time() < deadline and not action:
            action = manager_account_action(page, account_hint)
            if not action:
                time.sleep(0.25)
        if not action or not trusted_click(page, action):
            return None

        keywords = tuple(str(value).lower() for value in url_keywords if value)
        deadline = time.time() + max(1, timeout)
        reuse_deadline = time.time() + 5
        while time.time() < deadline:
            candidates = [
                item for item in list_targets(endpoint)
                if item.get("type") == "page"
                and item.get("webSocketDebuggerUrl")
                and any(keyword in str(item.get("url", "")).lower() for keyword in keywords)
            ]
            new_target = next(
                (item for item in candidates if item.get("id") not in existing),
                None,
            )
            if new_target:
                return new_target
            changed_target = next(
                (
                    item for item in candidates
                    if existing.get(item.get("id")) != str(item.get("url", ""))
                ),
                None,
            )
            if changed_target:
                return changed_target
            if candidates and time.time() >= reuse_deadline:
                return candidates[0]
            time.sleep(0.25)
    finally:
        connection.close()
    return None
