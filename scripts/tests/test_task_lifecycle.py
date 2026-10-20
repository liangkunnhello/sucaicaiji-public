#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import io
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import account_manager
import admuse_download
import appgrowing_download
import direct_cdp
import reyun_download
import task_browser


class AccountManagerTests(unittest.TestCase):
    class Row:
        def __init__(self, text, action):
            self.text = text
            self.action = action

        def inner_text(self):
            return self.text

        def query_selector(self, selector):
            return self.action if selector == ".action-btn" else None

    class Page:
        def __init__(self, rows):
            self.rows = rows

        def query_selector_all(self, selector):
            return self.rows if selector == "tbody tr" else []

    def test_account_hint_is_preferred_and_missing_hint_falls_back(self):
        first = object()
        design = object()
        page = self.Page([
            self.Row("其他账号", first),
            self.Row("设计部", design),
        ])

        self.assertIs(design, account_manager.manager_account_action(page, "设计部"))
        self.assertIs(first, account_manager.manager_account_action(page, "不存在"))


class TaskCleanupTests(unittest.TestCase):
    def test_target_added_during_task_is_preserved_at_cleanup(self):
        with patch.object(
            task_browser, "snapshot_target_ids", return_value={"manager"}
        ), patch.object(task_browser, "close_new_targets") as close_new:
            with task_browser.task_target_scope(
                "endpoint", "有米云", url_keywords=("appgrowing", "youcloud")
            ) as persistent_ids:
                persistent_ids.add("login-bootstrap")

        close_new.assert_called_once_with(
            "endpoint",
            {"manager", "login-bootstrap"},
            "有米云",
            url_keywords=("appgrowing", "youcloud"),
        )

    def test_cleanup_preserves_initial_tabs_and_manager_home(self):
        targets = [
            {"type": "page", "id": "existing", "url": "https://example.com"},
            {"type": "page", "id": "manager", "url": "chrome://newtab/"},
            {"type": "page", "id": "task", "url": "https://admuse.qq.com/#/idea"},
            {"type": "page", "id": "popup", "url": "https://xui.ptlogin2.qq.com/"},
            {"type": "page", "id": "unrelated", "url": "https://example.org/manual"},
        ]
        with patch.object(task_browser, "list_targets", return_value=targets), patch.object(
            task_browser, "close_owned_targets", return_value=2
        ) as close_owned:
            closed = task_browser.close_new_targets(
                "endpoint",
                {"existing"},
                "妙思",
                url_keywords=("admuse.qq.com", "ptlogin"),
            )

        self.assertEqual(2, closed)
        self.assertEqual(["task", "popup"], close_owned.call_args.args[1])


class DirectEvaluateCompatibilityTests(unittest.TestCase):
    def test_page_evaluate_forwards_playwright_style_arguments(self):
        connection = MagicMock()
        page = direct_cdp.DirectPage(connection, {"id": "task"})

        page.evaluate("(value) => value + 1", 41)

        connection.evaluate.assert_called_once_with("((value) => value + 1)(41)")

    def test_frame_evaluate_forwards_playwright_style_arguments(self):
        connection = MagicMock()
        parent = direct_cdp.DirectPage(connection, {"id": "task"})
        frame = direct_cdp.DirectFrame(parent, "frame", 7)

        frame.evaluate("(value) => value + 1", 41)

        connection.evaluate.assert_called_once_with(
            "((value) => value + 1)(41)", context_id=7
        )

    def test_element_evaluate_keeps_element_before_extra_arguments(self):
        connection = MagicMock()
        page = direct_cdp.DirectPage(connection, {"id": "task"})
        element = direct_cdp.DirectElement(page, "[document.body]", 0)

        element.evaluate("(el, value) => value", "payload")

        connection.evaluate.assert_called_once_with(
            '((el, value) => value)((([document.body])[0]), "payload")'
        )

    def test_direct_cdp_closes_only_requested_target(self):
        target = {"id": "task", "webSocketDebuggerUrl": "ws://task"}
        connection = MagicMock()
        with patch.object(direct_cdp, "list_targets", return_value=[target]), patch.object(
            direct_cdp, "CDPConnection", return_value=connection
        ):
            self.assertTrue(direct_cdp.close_target("endpoint", "task"))

        connection.call.assert_called_once_with("Page.close")
        connection.close.assert_called_once_with()

    def test_task_target_is_created_in_source_browser_context(self):
        connection = MagicMock()
        connection.call.side_effect = [
            {"targetInfo": {"browserContextId": "design-context"}},
            {"targetId": "new-task"},
        ]
        created_target = {
            "id": "new-task",
            "webSocketDebuggerUrl": "ws://new-task",
        }
        version = io.BytesIO(b'{"webSocketDebuggerUrl":"ws://browser"}')

        with patch.object(direct_cdp.urllib.request, "urlopen", return_value=version), patch.object(
            direct_cdp, "CDPConnection", return_value=connection
        ) as cdp, patch.object(direct_cdp, "list_targets", return_value=[created_target]):
            result = direct_cdp.open_target_in_context(
                "http://127.0.0.1:9223",
                "https://appgrowing-cn.youcloud.com/",
                "source",
            )

        self.assertIs(created_target, result)
        cdp.assert_called_once_with(
            "ws://browser", timeout=10.0, enable_page_domains=False
        )
        self.assertEqual(
            (
                "Target.createTarget",
                {
                    "url": "https://appgrowing-cn.youcloud.com/",
                    "browserContextId": "design-context",
                },
            ),
            connection.call.call_args_list[1].args,
        )


class AdmuseIndustryTests(unittest.TestCase):
    def test_persisted_industry_selection_is_cleared(self):
        page = MagicMock()
        page.evaluate.return_value = True
        with patch.object(admuse_download.time, "sleep"):
            self.assertTrue(admuse_download.clear_industry_selection(page))

        script = page.evaluate.call_args.args[0]
        self.assertIn(".spaui-select-tree .spaui-select-clear", script)
        self.assertIn("clear.click()", script)


class AppgrowingTabStateTests(unittest.TestCase):
    def test_minimized_account_manager_window_is_restored(self):
        page = MagicMock()
        page.target = {"id": "appgrowing-target"}
        response = MagicMock()
        response.json.return_value = {"webSocketDebuggerUrl": "ws://browser"}
        browser_connection = MagicMock()
        browser_connection.call.side_effect = [
            {"windowId": 17, "bounds": {"windowState": "minimized", "width": 1815, "height": 1197}},
            {},
            {"windowId": 17, "bounds": {"windowState": "normal", "width": 1815, "height": 1197}},
        ]

        with patch.object(appgrowing_download.requests, "get", return_value=response), patch.object(
            appgrowing_download, "CDPConnection", return_value=browser_connection
        ):
            self.assertTrue(appgrowing_download.ensure_appgrowing_window(page))

        browser_connection.call.assert_any_call(
            "Browser.setWindowBounds",
            {"windowId": 17, "bounds": {"windowState": "normal"}},
        )

    def test_collapsed_viewport_is_repaired_and_verified(self):
        collapsed = {
            "width": 408,
            "height": 42,
            "visualWidth": 408,
            "visualHeight": 42,
        }
        restored = {
            "width": 1280,
            "height": 900,
            "visualWidth": 1280,
            "visualHeight": 900,
        }

        class Page:
            connection = MagicMock()

            def __init__(self):
                self.metrics = [collapsed, collapsed, restored]

            def evaluate(self, _script):
                return self.metrics.pop(0)

        page = Page()
        with patch.object(
            appgrowing_download, "ensure_appgrowing_window", return_value=False
        ), patch.object(appgrowing_download.time, "sleep"):
            self.assertTrue(
                appgrowing_download.ensure_appgrowing_viewport(page, attempts=1)
            )

        page.connection.call.assert_any_call("Emulation.clearDeviceMetricsOverride")
        page.connection.call.assert_any_call(
            "Emulation.setPageScaleFactor", {"pageScaleFactor": 1}
        )
        page.connection.call.assert_any_call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 1280,
                "height": 900,
                "deviceScaleFactor": 1,
                "mobile": False,
                "screenWidth": 1280,
                "screenHeight": 900,
                "scale": 1,
            },
        )

    def test_viewport_repair_reports_failure_when_metrics_do_not_change(self):
        class Page:
            connection = MagicMock()

            def evaluate(self, _script):
                return {
                    "width": 408,
                    "height": 42,
                    "visualWidth": 408,
                    "visualHeight": 42,
                }

        with patch.object(
            appgrowing_download, "ensure_appgrowing_window", return_value=False
        ), patch.object(appgrowing_download.time, "sleep"):
            self.assertFalse(
                appgrowing_download.ensure_appgrowing_viewport(Page(), attempts=1)
            )

    def test_usable_viewport_does_not_apply_emulation(self):
        page = MagicMock()
        page.evaluate.return_value = {
            "width": 1280,
            "height": 900,
            "visualWidth": 1280,
            "visualHeight": 900,
        }
        with patch.object(
            appgrowing_download, "ensure_appgrowing_window", return_value=False
        ):
            self.assertTrue(appgrowing_download.ensure_appgrowing_viewport(page))

        page.connection.call.assert_not_called()

    def test_viewport_is_repaired_again_when_navigation_collapses_it(self):
        collapsed = {
            "width": 408,
            "height": 42,
            "visualWidth": 408,
            "visualHeight": 42,
        }
        restored = {
            "width": 1280,
            "height": 900,
            "visualWidth": 1280,
            "visualHeight": 900,
        }
        page = object()
        with patch.object(
            appgrowing_download,
            "_read_appgrowing_viewport",
            side_effect=[collapsed, restored, restored],
        ), patch.object(
            appgrowing_download, "ensure_appgrowing_viewport", return_value=True
        ) as repair, patch.object(appgrowing_download.time, "sleep"):
            self.assertTrue(
                appgrowing_download.wait_for_stable_appgrowing_viewport(
                    page, timeout=1, stable_reads=2
                )
            )

        repair.assert_called_once_with(page, force=True, attempts=2)

    def test_per_tab_authentication_state_is_cloned_before_reload(self):
        source = MagicMock()
        source.evaluate.return_value = {
            "localStorage": {"local-key": "local-value"},
            "sessionStorage": {"session-key": "session-value"},
            "windowName": "account-context",
        }
        task = MagicMock()

        appgrowing_download.clone_appgrowing_tab_state(source, task)

        script = task.evaluate.call_args.args[0]
        self.assertIn("localStorage.setItem", script)
        self.assertIn("sessionStorage.setItem", script)
        self.assertIn("window.name", script)
        self.assertIn("location.reload()", script)
        task.wait_for_load_state.assert_called_once_with("networkidle", timeout=15000)

    def test_authenticated_page_opens_task_tab_as_its_opener(self):
        source = MagicMock()
        with patch.object(
            appgrowing_download,
            "list_targets",
            side_effect=[
                [{"id": "source"}],
                [{
                    "id": "task",
                    "type": "page",
                    "url": "https://appgrowing-cn.youcloud.com/",
                    "webSocketDebuggerUrl": "ws://task",
                }],
            ],
        ):
            target = appgrowing_download.open_appgrowing_task_target(source)

        self.assertEqual("task", target["id"])
        self.assertIn("window.open", source.evaluate.call_args.args[0])


class ReyunLoginTests(unittest.TestCase):
    class Agreement:
        def __init__(self):
            self.checked = False

        def evaluate(self, _script):
            return self.checked

        def click(self):
            self.checked = True

    def test_agreement_is_checked_idempotently(self):
        agreement = self.Agreement()
        page = MagicMock()
        page.query_selector.return_value = agreement
        with patch.object(reyun_download.time, "sleep"):
            self.assertTrue(reyun_download.ensure_reyun_login_agreement(page))
            self.assertTrue(reyun_download.ensure_reyun_login_agreement(page))

    def test_prefilled_login_form_is_submitted(self):
        class Submit:
            def __init__(self, page):
                self.page = page
                self.clicked = False

            def is_visible(self):
                return True

            def click(self):
                self.clicked = True
                self.page.url = "https://data.insightrackr.com/"

        class Page:
            def __init__(self):
                self.url = "https://data.insightrackr.com/login?redirect=/"
                self.submit = Submit(self)

            def query_selector(self, selector):
                return self.submit if selector == "button.submit" else None

        page = Page()
        with patch.object(reyun_download, "accept_reyun_cookie_notice"), patch.object(
            reyun_download,
            "reyun_login_form_state",
            return_value={"accountReady": True, "passwordReady": True},
        ), patch.object(
            reyun_download, "ensure_reyun_login_agreement", return_value=True
        ), patch.object(reyun_download.time, "sleep"):
            self.assertTrue(reyun_download.ensure_reyun_logged_in(page, timeout=1))

        self.assertTrue(page.submit.clicked)


if __name__ == "__main__":
    unittest.main()
