import sys
import unittest
import os
import tempfile

import _tempdir  # noqa: F401  # 把临时目录挪出 %TEMP%（见 _tempdir.py）
from pathlib import Path
from unittest.mock import MagicMock, patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import appgrowing_download


class FakeCreativeEntry:
    def __init__(self, page, text="创意"):
        self.page = page
        self.text = text
        self.clicked = False

    def inner_text(self):
        return self.text

    def evaluate(self, _script):
        self.clicked = True
        self.page.ready = True
        self.page.url = "https://appgrowing-cn.youcloud.com/leaflet/list"


class FakePage:
    def __init__(self, ready=False, with_entry=True):
        self.ready = ready
        self.url = (
            "https://appgrowing-cn.youcloud.com/leaflet/list"
            if ready
            else "https://appgrowing-cn.youcloud.com/dashboard"
        )
        self.entry = FakeCreativeEntry(self) if with_entry else None

    def evaluate(self, script):
        if "location.pathname.startsWith('/leaflet/list')" in script:
            return self.ready
        raise AssertionError(f"unexpected script: {script}")

    def query_selector_all(self, _selector):
        return [self.entry] if self.entry else []


class CreativeNavigationTests(unittest.TestCase):
    def test_existing_creative_filter_page_does_not_click_navigation(self):
        page = FakePage(ready=True)

        self.assertTrue(appgrowing_download.ensure_creative_page(page, timeout=1))
        self.assertFalse(page.entry.clicked)

    def test_dashboard_clicks_top_creative_entry_and_waits_until_ready(self):
        page = FakePage(ready=False)

        self.assertTrue(
            appgrowing_download.ensure_creative_page(page, timeout=1, poll_interval=0.01)
        )
        self.assertTrue(page.entry.clicked)
        self.assertTrue(page.ready)

    def test_missing_creative_entry_fails_instead_of_running_filters(self):
        page = FakePage(ready=False, with_entry=False)

        self.assertFalse(appgrowing_download.ensure_creative_page(page, timeout=1))


class CaptureRetryTests(unittest.TestCase):
    def test_transient_capture_failure_is_retried_once(self):
        with patch.object(
            appgrowing_download,
            "main",
            side_effect=[RuntimeError("页面暂时不可用"), None],
        ) as capture, patch.object(appgrowing_download.time, "sleep") as sleep:
            appgrowing_download.run_capture_with_retry(
                [], [], "保险", "全部素材比例", 1, "", "图片", "广告"
            )

        self.assertEqual(2, capture.call_count)
        sleep.assert_called_once_with(3)

    def test_final_capture_failure_is_raised_after_retry(self):
        with patch.object(
            appgrowing_download,
            "main",
            side_effect=RuntimeError("登录恢复超时"),
        ) as capture, patch.object(appgrowing_download.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "登录恢复超时"):
                appgrowing_download.run_capture_with_retry(
                    [], [], "保险", "全部素材比例", 1, "", "图片", "广告"
                )

        self.assertEqual(2, capture.call_count)


class MaterialSelectorTests(unittest.TestCase):
    def test_current_and_legacy_material_containers_are_supported(self):
        class Page:
            script = ""

            def evaluate(self, script):
                self.script = script
                return ["https://creative-static-ag-v2.umcdn.cn/poster/test.jpg"]

        page = Page()
        urls = appgrowing_download.collect_page_materials(page)

        self.assertEqual(1, len(urls))
        self.assertIn(".agd-ad-material-v2__content,", page.script)
        self.assertIn(".agd-ad-material-v2__content-container", page.script)

    def test_material_type_is_applied_after_ratio_and_sort(self):
        actions = []

        def category(_page, entry, option):
            actions.append((entry, option))
            return True

        def sort(_page, option):
            actions.append(("排序", option))
            return True

        with patch.object(
            appgrowing_download, "select_category_option", side_effect=category
        ), patch.object(appgrowing_download, "select_sort_option", side_effect=sort):
            applied = appgrowing_download.apply_advanced_filters(
                object(), "竖版", "按今日曝光数降序", "图片"
            )

        self.assertTrue(applied)
        self.assertEqual(
            [
                ("全部素材比例", "竖版"),
                ("排序", "按今日曝光数降序"),
                ("全部素材类型", "图片"),
            ],
            actions,
        )

    def test_existing_material_type_is_not_toggled_off(self):
        current = LoginElement("图片")
        ratio = LoginElement("全部素材比例")

        class Keyboard:
            def press(self, _key):
                return None

        class Page:
            keyboard = Keyboard()

            def query_selector_all(self, selector):
                self.selector = selector
                return [current, ratio]

        with patch.object(appgrowing_download.time, "sleep"):
            selected = appgrowing_download.select_category_option(
                Page(), "全部素材类型", "图片"
            )

        self.assertTrue(selected)
        self.assertFalse(current.clicked)

    def test_filter_reset_clicks_visible_clear_button(self):
        class ClearButton(LoginElement):
            def evaluate(self, script):
                if "getBoundingClientRect().width" in script:
                    return True
                if "el.click()" in script:
                    self.clicked = True
                    return None
                return super().evaluate(script)

        clear = ClearButton("清空")

        class Page:
            def query_selector_all(self, selector):
                self.selector = selector
                return [clear]

            def wait_for_load_state(self, _state, timeout):
                self.timeout = timeout

        page = Page()
        with patch.object(appgrowing_download.time, "sleep"):
            reset = appgrowing_download.reset_appgrowing_filters(page)

        self.assertTrue(reset)
        self.assertTrue(clear.clicked)
        self.assertEqual(".operation-wrapper button.ymc-button", page.selector)

    def test_hidden_clear_button_means_filters_are_already_clean(self):
        class HiddenClear(LoginElement):
            def evaluate(self, script):
                if "getBoundingClientRect().width" in script:
                    return False
                return super().evaluate(script)

        clear = HiddenClear("清空")

        class Page:
            def query_selector_all(self, _selector):
                return [clear]

        self.assertTrue(appgrowing_download.reset_appgrowing_filters(Page()))
        self.assertFalse(clear.clicked)


class LoginElement:
    def __init__(self, text="", checked=False, placeholder=None):
        self.text = text
        self.checked = checked
        self.placeholder = placeholder
        self.clicked = False

    def inner_text(self):
        return self.text

    def get_attribute(self, name):
        return self.placeholder if name == "placeholder" else None

    def evaluate(self, script):
        if "getBoundingClientRect().width" in script:
            return True
        if "Boolean(el.checked)" in script:
            return self.checked
        raise AssertionError(f"unexpected script: {script}")


class LoginRecoveryTests(unittest.TestCase):
    def test_existing_logged_in_target_is_refreshed_and_reused(self):
        target = {
            "id": "logged-in",
            "type": "page",
            "url": "https://appgrowing-cn.youcloud.com/dashboard",
            "webSocketDebuggerUrl": "ws://logged-in",
        }
        events = []
        connection = MagicMock()
        connection.call.side_effect = lambda *_args, **_kwargs: events.append("reload")
        page = type("Page", (), {"target": target, "connection": connection})()
        page.evaluate = lambda *_args, **_kwargs: (
            events.append("loaded")
            or {"readyState": "interactive", "bodyReady": True}
        )
        browser = type(
            "Browser",
            (),
            {"contexts": [type("Context", (), {"pages": [page]})()]},
        )()
        response = type(
            "Response",
            (),
            {
                "raise_for_status": lambda self: None,
                "json": lambda self: {
                    "webSocketDebuggerUrl": "ws://browser",
                    "User-Agent": "OrayOnion/test",
                },
            },
        )()
        persistent_ids = set()

        with patch("cdp_guard.ensure_cdp", return_value=True), patch.object(
            appgrowing_download.requests, "get", return_value=response
        ), patch.object(
            appgrowing_download, "list_targets", return_value=[target]
        ), patch.object(
            appgrowing_download, "_direct_browser_for_target", return_value=browser
        ), patch.object(
            appgrowing_download,
            "is_appgrowing_application_page",
            side_effect=lambda _page: events.append("checked") or True,
        ), patch.object(
            appgrowing_download, "ensure_appgrowing_viewport", return_value=False
        ), patch.object(appgrowing_download, "open_target") as open_home, patch.object(
            appgrowing_download.time, "sleep"
        ):
            result = appgrowing_download.connect_browser(object(), persistent_ids)

        self.assertIs(browser, result)
        self.assertEqual({"logged-in"}, persistent_ids)
        self.assertEqual(
            ["reload", "loaded", "loaded", "loaded"] + ["checked"] * 5,
            events,
        )
        connection.call.assert_called_once_with(
            "Page.reload", {"ignoreCache": False}
        )
        open_home.assert_not_called()

    def test_logged_out_target_is_reused_for_direct_web_login(self):
        stale_target = {
            "id": "stale",
            "type": "page",
            "url": "https://appgrowing-cn.youcloud.com/dashboard",
            "webSocketDebuggerUrl": "ws://stale",
        }
        stale_connection = MagicMock()
        stale_page = type(
            "Page",
            (),
            {
                "target": stale_target,
                "connection": stale_connection,
                "evaluate": lambda *_args, **_kwargs: {
                    "readyState": "interactive",
                    "bodyReady": True,
                },
            },
        )()
        stale_browser = type(
            "Browser",
            (),
            {
                "contexts": [type("Context", (), {"pages": [stale_page]})()],
                "close": MagicMock(),
            },
        )()
        response = type(
            "Response",
            (),
            {
                "raise_for_status": lambda self: None,
                "json": lambda self: {
                    "webSocketDebuggerUrl": "ws://browser",
                    "User-Agent": "OrayOnion/test",
                },
            },
        )()
        persistent_ids = set()

        with patch("cdp_guard.ensure_cdp", return_value=True), patch.object(
            appgrowing_download.requests, "get", return_value=response
        ), patch.object(
            appgrowing_download, "list_targets", return_value=[stale_target]
        ), patch.object(
            appgrowing_download,
            "_direct_browser_for_target",
            return_value=stale_browser,
        ), patch.object(
            appgrowing_download, "is_appgrowing_application_page", return_value=False
        ), patch.object(
            appgrowing_download, "ensure_appgrowing_viewport", return_value=False
        ), patch.object(appgrowing_download, "open_target") as open_home, patch.object(
            appgrowing_download.time, "sleep"
        ):
            result = appgrowing_download.connect_browser(object(), persistent_ids)

        self.assertIs(stale_browser, result)
        self.assertEqual({"stale"}, persistent_ids)
        stale_connection.call.assert_called_once_with(
            "Page.reload", {"ignoreCache": False}
        )
        stale_browser.close.assert_not_called()
        open_home.assert_not_called()

    def test_missing_target_opens_official_home_directly(self):
        target = {
            "id": "new-home",
            "type": "page",
            "url": "https://appgrowing-cn.youcloud.com/",
            "webSocketDebuggerUrl": "ws://new-home",
        }
        browser = object()
        response = type(
            "Response",
            (),
            {"raise_for_status": lambda self: None},
        )()
        persistent_ids = set()

        with patch("cdp_guard.ensure_cdp", return_value=True), patch.object(
            appgrowing_download.requests, "get", return_value=response
        ), patch.object(
            appgrowing_download, "list_targets", return_value=[]
        ), patch.object(
            appgrowing_download, "open_target", return_value=target
        ) as open_home, patch.object(
            appgrowing_download, "_direct_browser_for_target", return_value=browser
        ):
            result = appgrowing_download.connect_browser(object(), persistent_ids)

        self.assertIs(browser, result)
        self.assertEqual({"new-home"}, persistent_ids)
        open_home.assert_called_once_with(
            appgrowing_download.CDP_ENDPOINT,
            appgrowing_download.APPGROWING_HOME_URL,
            timeout=10,
        )

    def test_auth_url_is_recognized_as_logged_out(self):
        class Page:
            url = (
                "https://auth.youcloud.com/login?app_id=appgrowing"
                "&goto=https%3A%2F%2Fappgrowing-cn.youcloud.com%2Fdashboard"
            )

        self.assertTrue(appgrowing_download.is_appgrowing_login_page(Page()))
        self.assertFalse(appgrowing_download.is_appgrowing_application_page(Page()))

    def test_app_url_is_recognized_after_login(self):
        class Page:
            url = "https://appgrowing-cn.youcloud.com/dashboard"

            def query_selector(self, _selector):
                return None

        self.assertTrue(appgrowing_download.is_appgrowing_application_page(Page()))

    def test_app_url_with_visible_signin_is_recognized_as_logged_out(self):
        class Page:
            url = "https://appgrowing-cn.youcloud.com/dashboard"

            def query_selector(self, selector):
                self.selector = selector
                return LoginElement("登录")

        page = Page()
        self.assertFalse(appgrowing_download.is_appgrowing_application_page(page))
        self.assertEqual("button.signin", page.selector)

    def test_guest_page_opens_auth_login(self):
        sign_in = LoginElement("登录")

        class Page:
            url = "https://appgrowing-cn.youcloud.com/dashboard"

            def query_selector(self, _selector):
                return sign_in

        page = Page()

        def click(_page, element):
            element.clicked = True
            page.url = "https://auth.youcloud.com/login?app_id=appgrowing"
            return True

        with patch.object(
            appgrowing_download, "_trusted_click", side_effect=click
        ), patch.object(
            appgrowing_download,
            "wait_for_stable_appgrowing_viewport",
            return_value=True,
        ) as stable_viewport:
            self.assertTrue(appgrowing_download.open_appgrowing_login(page, timeout=1))

        self.assertTrue(sign_in.clicked)
        stable_viewport.assert_called_once_with(page)

    def test_guest_page_falls_back_to_official_auth_url(self):
        sign_in = LoginElement("登录")

        class Page:
            url = "https://appgrowing-cn.youcloud.com/dashboard"

            def query_selector(self, _selector):
                return sign_in

            def evaluate(self, script):
                if "location.assign" in script:
                    self.url = appgrowing_download.APPGROWING_LOGIN_URL
                    return None
                raise AssertionError(f"unexpected script: {script}")

        page = Page()
        with patch.object(
            appgrowing_download, "_trusted_click", return_value=True
        ), patch.object(
            appgrowing_download,
            "is_appgrowing_application_page",
            return_value=False,
        ), patch.object(
            appgrowing_download,
            "wait_for_stable_appgrowing_viewport",
            return_value=True,
        ), patch.object(appgrowing_download.time, "sleep"):
            self.assertTrue(
                appgrowing_download.open_appgrowing_login(page, timeout=0.01)
            )

        self.assertEqual(appgrowing_download.APPGROWING_LOGIN_URL, page.url)

    def test_email_input_uses_ascii_selector_and_placeholder_property(self):
        phone = LoginElement(placeholder="请输入手机号码")
        email = LoginElement(placeholder="请输入邮箱")

        class Page:
            def query_selector_all(self, selector):
                self.selector = selector
                return [phone, email]

        page = Page()
        self.assertIs(email, appgrowing_download._find_email_input(page))
        self.assertEqual("input.ymc-input__inner[type='text']", page.selector)

    def test_account_hint_selects_design_account(self):
        other = LoginElement("策略二组 账密登录")
        design = LoginElement("设计部 账密登录")

        class Page:
            def query_selector_all(self, selector):
                self.selector = selector
                return [other, design]

        def click(_page, element):
            element.clicked = True
            return True

        with patch.object(appgrowing_download, "_trusted_click", side_effect=click):
            self.assertTrue(appgrowing_download._select_account_manager_login(Page()))

        self.assertTrue(design.clicked)
        self.assertFalse(other.clicked)

    def test_agreement_uses_last_checkbox(self):
        remember_me = LoginElement(checked=True)
        agreement = LoginElement(checked=False)

        class Page:
            def query_selector_all(self, selector):
                self.selector = selector
                return [remember_me, agreement]

        def click(_page, element):
            element.checked = True
            return True

        with patch.object(appgrowing_download, "_trusted_click", side_effect=click):
            self.assertTrue(appgrowing_download._ensure_login_agreement(Page()))

        self.assertTrue(agreement.checked)


class JfbymTokenTests(unittest.TestCase):
    def test_two_line_or_labeled_token_file_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token.txt"
            token_file.write_text("# local only\ntoken=test-token\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"JFBYM_TOKEN": "", "JFBYM_TOKEN_FILE": str(token_file)},
                clear=False,
            ):
                token, source = appgrowing_download.load_jfbym_token()

        self.assertEqual("test-token", token)
        self.assertEqual("file", source)

    def test_environment_token_has_priority(self):
        with patch.dict(os.environ, {"JFBYM_TOKEN": "env-token"}, clear=False):
            token, source = appgrowing_download.load_jfbym_token()

        self.assertEqual("env-token", token)
        self.assertEqual("environment", source)


class JfbymTrajectoryTests(unittest.TestCase):
    def test_trajectory_response_is_parsed(self):
        points = appgrowing_download.parse_jfbym_trajectory(
            "206,112|205,112|202,113|198,115|190,116"
        )

        self.assertEqual((206.0, 112.0), points[0])
        self.assertEqual((190.0, 116.0), points[-1])

    def test_reversed_source_path_maps_to_forward_slider(self):
        mapped = appgrowing_download._normalize_slider_trajectory(
            [(20, 10), (16, 13), (10, 8), (0, 10)],
            start_x=100,
            start_y=50,
            travel=200,
        )

        self.assertEqual((100, 50), mapped[0])
        self.assertEqual((302, 50), mapped[-1])
        self.assertNotEqual(50, mapped[1][1])

    def test_slider_result_is_checked_after_mouse_release(self):
        events = []

        class Page:
            def bring_to_front(self):
                return None

            def evaluate(self, script):
                if "const knob" in script:
                    return {"startX": 100, "startY": 50, "travel": 200}
                self.assert_released()
                return True

            @staticmethod
            def assert_released():
                if not events or events[-1] != "mouseReleased":
                    raise AssertionError("slider state checked before mouse release")

        def record_event(_page, event_type, _x, _y, pressed=False):
            events.append(event_type)

        with patch.object(appgrowing_download, "_dispatch_mouse", side_effect=record_event), patch.object(
            appgrowing_download.time, "sleep"
        ):
            passed = appgrowing_download._drag_slider_with_trajectory(
                Page(), [(20, 10), (16, 13), (10, 8), (0, 10)]
            )

        self.assertTrue(passed)
        self.assertEqual("mouseReleased", events[-1])

    def test_api_request_uses_type_100018(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "code": 10000,
                    "data": {"code": 0, "data": "8,2|7,3|5,4|2,4"},
                }

        class Client:
            payload = None

            def post(self, _url, **kwargs):
                self.payload = kwargs["json"]
                return Response()

        client = Client()
        points = appgrowing_download.request_jfbym_trajectory(
            b"png-bytes", token="private-token", http_client=client
        )

        self.assertEqual("100018", client.payload["type"])
        self.assertEqual("private-token", client.payload["token"])
        self.assertEqual(4, len(points))

    def test_solver_stays_manual_without_token(self):
        missing_file = str(Path(tempfile.gettempdir()) / "missing-jfbym-token.txt")
        with patch.dict(
            os.environ,
            {"JFBYM_TOKEN": "", "JFBYM_TOKEN_FILE": missing_file},
            clear=False,
        ):
            self.assertEqual(
                "not_configured",
                appgrowing_download.try_jfbym_slider_login(None),
            )

    def test_solver_accepts_delayed_application_redirect(self):
        with patch.object(
            appgrowing_download, "load_jfbym_token", return_value=("token", "file")
        ), patch.object(
            appgrowing_download, "_capture_slider_image", return_value=b"png"
        ), patch.object(
            appgrowing_download,
            "request_jfbym_trajectory",
            return_value=[(4, 1), (3, 2), (2, 1), (0, 1)],
        ), patch.object(
            appgrowing_download, "_drag_slider_with_trajectory", return_value=False
        ), patch.object(
            appgrowing_download,
            "is_appgrowing_application_page",
            side_effect=[False, True],
        ), patch.object(
            appgrowing_download, "appgrowing_login_form_state", return_value={}
        ), patch.object(appgrowing_download.time, "sleep"):
            status = appgrowing_download.try_jfbym_slider_login(object())

        self.assertEqual("submitted", status)


class SortCompatibilityTests(unittest.TestCase):
    """素材类型与排序的互斥关系。

    2026-09-25 线上故障：广告视图默认「按视频播放数降序」，该视图结果又全是视频卡，
    计划里再把类型筛成「图片」→ 交集为空 → 页面一条不返回 → 整批失败。
    实测换任何不依赖视频的排序都能正常出图，故加了自动兜底。
    """

    def test_image_type_avoids_video_sort(self):
        self.assertEqual(
            appgrowing_download.FALLBACK_SORT,
            appgrowing_download.resolve_sort("", "广告", "图片"),
        )

    def test_all_image_like_types_are_covered(self):
        for mtype in ("图片", "图标", "轮播图", "组图", "GIF", "纯文案", "网页"):
            with self.subTest(mtype=mtype):
                self.assertEqual(
                    appgrowing_download.FALLBACK_SORT,
                    appgrowing_download.resolve_sort("按视频播放数降序", "广告", mtype),
                )

    def test_video_types_keep_video_sort(self):
        for mtype in ("视频", "竖视频", "全屏视频", "全部素材类型"):
            with self.subTest(mtype=mtype):
                self.assertEqual(
                    "按视频播放数降序",
                    appgrowing_download.resolve_sort("", "广告", mtype),
                )

    def test_explicit_video_sort_is_overridden_for_images(self):
        self.assertEqual(
            appgrowing_download.FALLBACK_SORT,
            appgrowing_download.resolve_sort("按视频点赞数降序", "广告", "图片"),
        )

    def test_sort_alias_is_resolved_before_the_check(self):
        # 「按视频曝光数降序」是「按视频播放数降序」的别名，同样必须被拦下
        self.assertEqual(
            appgrowing_download.FALLBACK_SORT,
            appgrowing_download.resolve_sort("按视频曝光数降序", "广告", "图片"),
        )

    def test_neutral_sort_is_left_alone(self):
        self.assertEqual(
            "按投放时间降序",
            appgrowing_download.resolve_sort("按投放时间降序", "广告", "图片"),
        )

    def test_material_view_default_sort_is_not_video_based(self):
        # 素材视图默认「按关联广告创意数降序」，与图片类型并不冲突，不该被改
        self.assertEqual(
            "按关联广告创意数降序",
            appgrowing_download.resolve_sort("", "素材", "图片"),
        )


class ResultTotalTests(unittest.TestCase):
    """结果总数解析：用于在「共 0 条」时提前收工，不白等满超时。"""

    def _page(self, value):
        page = MagicMock()
        page.evaluate.return_value = value
        return page

    def test_parses_total_with_thousands_separator(self):
        self.assertEqual(35873845, appgrowing_download.read_result_total(self._page("35873845")))

    def test_returns_zero_for_empty_result(self):
        self.assertEqual(0, appgrowing_download.read_result_total(self._page("0")))

    def test_returns_none_when_absent(self):
        self.assertIsNone(appgrowing_download.read_result_total(self._page("")))

    def test_returns_none_on_evaluate_failure(self):
        page = MagicMock()
        page.evaluate.side_effect = RuntimeError("boom")
        self.assertIsNone(appgrowing_download.read_result_total(page))

    def test_returns_none_on_garbage(self):
        self.assertIsNone(appgrowing_download.read_result_total(self._page("abc")))


class WaitForResultCardsTests(unittest.TestCase):
    """列表懒渲染：不能读太早，也不能在「共 0 条」时白等。"""

    def test_returns_immediately_when_cards_exist(self):
        page = MagicMock()
        with patch.object(
            appgrowing_download, "collect_page_materials", return_value=["u1", "u2"]
        ), patch.object(appgrowing_download.time, "sleep") as sleep:
            urls = appgrowing_download.wait_for_result_cards(page, timeout=5, interval=0.01)
        self.assertEqual(["u1", "u2"], urls)
        sleep.assert_not_called()

    def test_exits_early_when_page_reports_zero(self):
        page = MagicMock()
        with patch.object(
            appgrowing_download, "collect_page_materials", return_value=[]
        ), patch.object(
            appgrowing_download, "read_result_total", return_value=0
        ), patch.object(appgrowing_download.time, "sleep") as sleep:
            urls = appgrowing_download.wait_for_result_cards(page, timeout=30, interval=0.01)
        self.assertEqual([], urls)
        sleep.assert_not_called()

    def test_keeps_polling_until_cards_appear(self):
        page = MagicMock()
        # 前两次读不到卡片（列表还没渲染），第三次才出现
        with patch.object(
            appgrowing_download,
            "collect_page_materials",
            side_effect=[[], [], ["ready"]],
        ), patch.object(
            appgrowing_download, "read_result_total", return_value=120
        ), patch.object(appgrowing_download.time, "sleep") as sleep:
            urls = appgrowing_download.wait_for_result_cards(page, timeout=30, interval=0.01)
        self.assertEqual(["ready"], urls)
        self.assertEqual(2, sleep.call_count)


class CrossRunDedupTests(unittest.TestCase):
    """同一天多次采集共用一个目录，按 URL 尾段跨采集去重。

    文件名带序号（0001、0002…），一旦两次结果顺序不同，
    同一张图会被存成两个重复文件 —— 所以判据只能用与序号无关的 URL 尾段。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _touch(self, name):
        (self.base / name).write_bytes(b"x")

    def test_extracts_token_from_standard_filename(self):
        self._touch("[有米云]_20260928_0001_abc123.jpeg.jpg")
        tokens = appgrowing_download.collect_existing_url_tokens(self.base)
        self.assertEqual({"abc123.jpeg"}, tokens)

    def test_token_ignores_sequence_number(self):
        # 同一张图落在不同序号位置，尾段必须一致，否则去重会失效
        self._touch("[有米云]_20260928_0001_abc123.jpeg.jpg")
        self._touch("[有米云]_20260928_0007_abc123.jpeg.jpg")
        tokens = appgrowing_download.collect_existing_url_tokens(self.base)
        self.assertEqual(1, len(tokens))

    def test_ignores_unrelated_files(self):
        self._touch("其他平台_0001_x.jpg")
        self._touch("capture-items.json")
        self.assertEqual(set(), appgrowing_download.collect_existing_url_tokens(self.base))

    def test_missing_directory_returns_empty(self):
        missing = self.base / "不存在"
        self.assertEqual(set(), appgrowing_download.collect_existing_url_tokens(missing))

    def test_multiple_entries_are_all_collected(self):
        self._touch("[有米云]_20260928_0001_aaa.jpg")
        self._touch("[有米云]_20260928_0002_bbb.png.jpg")
        tokens = appgrowing_download.collect_existing_url_tokens(self.base)
        self.assertEqual({"aaa", "bbb.png"}, tokens)


if __name__ == "__main__":
    unittest.main()
