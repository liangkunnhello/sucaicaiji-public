#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Helpers for tabs owned by one collector invocation."""
from __future__ import annotations

from contextlib import contextmanager

from direct_cdp import close_target, list_targets


def snapshot_pages(context):
    return tuple(context.pages)


def close_new_pages(context, initial_pages, platform_label):
    initial_ids = {id(page) for page in initial_pages}
    closed = 0
    for page in reversed(list(context.pages)):
        if id(page) in initial_ids:
            continue
        try:
            page.close()
            closed += 1
        except Exception:
            pass
    print(f"[页面清理] {platform_label}已关闭本次任务创建的 {closed} 个标签页")
    return closed


def snapshot_target_ids(endpoint):
    return {
        str(item.get("id")) for item in list_targets(endpoint)
        if item.get("type") == "page" and item.get("id")
    }


def close_owned_targets(endpoint, target_ids, platform_label):
    closed = 0
    for target_id in reversed(list(dict.fromkeys(str(value) for value in target_ids if value))):
        try:
            if close_target(endpoint, target_id):
                closed += 1
        except Exception:
            pass
    print(f"[页面清理] {platform_label}已关闭本次任务创建的 {closed} 个标签页")
    return closed


def close_new_targets(endpoint, initial_target_ids, platform_label, url_keywords=()):
    """Close task-created tabs while always preserving the manager home page."""
    initial_ids = {str(value) for value in initial_target_ids if value}
    keywords = tuple(str(value).lower() for value in url_keywords if value)
    owned_ids = [
        str(item.get("id"))
        for item in list_targets(endpoint)
        if item.get("type") == "page"
        and item.get("id")
        and str(item.get("id")) not in initial_ids
        and str(item.get("url", "")) != "chrome://newtab/"
        and (
            not keywords
            or any(keyword in str(item.get("url", "")).lower() for keyword in keywords)
        )
    ]
    return close_owned_targets(endpoint, owned_ids, platform_label)


@contextmanager
def task_target_scope(endpoint, platform_label, url_keywords=()):
    initial_target_ids = snapshot_target_ids(endpoint)
    try:
        yield initial_target_ids
    finally:
        try:
            close_new_targets(
                endpoint,
                initial_target_ids,
                platform_label,
                url_keywords=url_keywords,
            )
        except Exception as error:
            print(f"[页面清理] {platform_label}任务标签清理失败：{error}")
