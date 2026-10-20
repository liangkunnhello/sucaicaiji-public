import sys
import unittest
import os
import tempfile

import _tempdir  # noqa: F401  # 把临时目录挪出 %TEMP%（见 _tempdir.py）
from pathlib import Path
from unittest.mock import call, patch

import requests


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import admuse_download
import cdp_guard


class AdmuseLoginStateTests(unittest.TestCase):
    def test_complete_application_ui_is_logged_in(self):
        state = admuse_download.classify_admuse_login_state(
            "https://admuse.qq.com/#/idea",
            "首页\n灵感\n资产\n图片\n行业\n综合得分",
            app_ready=True,
        )
        self.assertEqual("logged_in", state)

    def test_login_url_is_logged_out(self):
        state = admuse_download.classify_admuse_login_state(
            "https://xui.ptlogin2.qq.com/cgi-bin/xlogin",
            "",
        )
        self.assertEqual("logged_out", state)

    def test_exact_login_label_is_logged_out(self):
        for label in ("登录/注册", "扫码登录", "立即登录", "QQ登录", "QQ 登录", "微信登录"):
            with self.subTest(label=label):
                state = admuse_download.classify_admuse_login_state(
                    "https://admuse.qq.com/#/idea",
                    f"欢迎使用\n{label}\n广告创意",
                )
                self.assertEqual("logged_out", state)

    def test_login_overlay_wins_over_stale_application_ui(self):
        state = admuse_download.classify_admuse_login_state(
            "https://admuse.qq.com/#/idea",
            "首页\n灵感\n资产\n图片\n行业\n综合得分\n登录/注册\nQQ 登录",
            app_ready=True,
            login_required=True,
        )

        self.assertEqual("logged_out", state)

    def test_qq_login_labels_include_spaced_live_button(self):
        self.assertIn("QQ 登录", admuse_download.ADMUSE_QQ_LOGIN_LABELS)

    def test_loading_page_is_unknown(self):
        state = admuse_download.classify_admuse_login_state(
            "https://admuse.qq.com/#/idea",
            "页面加载中，请稍候",
        )
        self.assertEqual("unknown", state)

    def test_filtered_insurance_page_is_still_recognized_as_ready(self):
        ready = admuse_download.is_admuse_application_ready(
            "首页\n灵感\n资产\n图片\n行业\n保险服务\n投放版位\n近3天\n跑量得分降序\n更多",
            ("视频", "图片", "游戏试玩"),
        )

        self.assertTrue(ready)

    def test_login_word_inside_normal_content_is_not_logged_out(self):
        state = admuse_download.classify_admuse_login_state(
            "https://admuse.qq.com/#/idea",
            "首页\n灵感\n这是一条关于用户登录转化率的广告文案",
        )
        self.assertEqual("unknown", state)


class AutoLoginConfigurationTests(unittest.TestCase):
    def test_auto_login_is_opt_in_through_environment(self):
        with patch.dict(
            os.environ,
            {
                admuse_download.ADMUSE_QQ_USERNAME_ENV: "",
                admuse_download.ADMUSE_QQ_PASSWORD_ENV: "",
                admuse_download.ADMUSE_QQ_CREDENTIALS_FILE_ENV: os.path.join(
                    tempfile.gettempdir(), "missing-admuse-credentials.txt"
                ),
            },
            clear=False,
        ):
            self.assertFalse(admuse_download.attempt_admuse_qq_password_login(None))

    def test_two_line_credentials_file_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            credentials_file = Path(directory) / "credentials.txt"
            credentials_file.write_text("test-user\ntest-password\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    admuse_download.ADMUSE_QQ_USERNAME_ENV: "",
                    admuse_download.ADMUSE_QQ_PASSWORD_ENV: "",
                    admuse_download.ADMUSE_QQ_CREDENTIALS_FILE_ENV: str(credentials_file),
                },
                clear=False,
            ):
                username, password, source = admuse_download.load_admuse_qq_credentials()

        self.assertEqual("test-user", username)
        self.assertEqual("test-password", password)
        self.assertEqual("file", source)


class AdmuseLoginEntryTests(unittest.TestCase):
    def test_click_visible_label_prefers_interactive_ancestor(self):
        frame = unittest.mock.Mock()
        frame.evaluate.return_value = True

        self.assertTrue(admuse_download._click_visible_label(frame, ("登录/注册",)))

        script = frame.evaluate.call_args.args[0]
        self.assertIn("closest(interactiveSelector)", script)
        frame.get_by_text.assert_not_called()

    def test_qq_login_opens_outer_dialog_first(self):
        page = object()
        calls = []

        def click(_frame, labels):
            calls.append(labels)
            if labels == admuse_download.ADMUSE_LOGIN_OPEN_LABELS:
                return True
            return calls.count(admuse_download.ADMUSE_QQ_LOGIN_LABELS) >= 2

        with (
            patch.object(admuse_download, "_auth_frames", return_value=[object()]),
            patch.object(admuse_download, "_click_visible_label", side_effect=click),
        ):
            self.assertTrue(admuse_download.open_admuse_qq_login(page, timeout=0.5))

        self.assertEqual(
            [
                admuse_download.ADMUSE_QQ_LOGIN_LABELS,
                admuse_download.ADMUSE_LOGIN_OPEN_LABELS,
                admuse_download.ADMUSE_QQ_LOGIN_LABELS,
            ],
            calls,
        )


class CookieSyncTests(unittest.TestCase):
    class FakeContext:
        def cookies(self):
            return [
                {
                    "name": "admuse_session",
                    "value": "fresh-token",
                    "domain": ".admuse.qq.com",
                    "path": "/",
                },
                {
                    "name": "qq_auth",
                    "value": "fresh-auth",
                    "domain": ".qq.com",
                    "path": "/",
                },
            ]

    def test_browser_cookies_replace_stale_download_session(self):
        session = requests.Session()
        session.cookies.set("admuse_session", "stale-token", domain=".admuse.qq.com", path="/")

        count = admuse_download.sync_session_cookies(self.FakeContext(), session)

        self.assertEqual(2, count)
        self.assertEqual(
            "fresh-token",
            session.cookies.get("admuse_session", domain=".admuse.qq.com", path="/"),
        )
        self.assertEqual(
            "fresh-auth",
            session.cookies.get("qq_auth", domain=".qq.com", path="/"),
        )


class CardScoreCompatibilityTests(unittest.TestCase):
    def test_new_card_without_visible_score_uses_server_side_sorting(self):
        self.assertTrue(admuse_download.card_passes_score(None, 40))

    def test_visible_score_still_respects_local_threshold(self):
        self.assertFalse(admuse_download.card_passes_score(40, 40))
        self.assertTrue(admuse_download.card_passes_score(40.1, 40))


class CollectionSessionRecoveryTests(unittest.TestCase):
    class Page:
        def __init__(self):
            self.goto_calls = []

        def goto(self, url, **kwargs):
            self.goto_calls.append((url, kwargs))

    def test_recovery_reapplies_page_filters_and_cookies(self):
        page = self.Page()
        context = object()
        session = object()

        with (
            patch.object(admuse_download, "inspect_admuse_login", return_value={"state": "logged_out"}),
            patch.object(admuse_download, "ensure_admuse_logged_in", return_value=True) as ensure,
            patch.object(admuse_download, "click_image_tab", return_value=True) as image_tab,
            patch.object(admuse_download, "set_time_range", return_value=True) as time_range,
            patch.object(admuse_download, "set_industry", return_value=True) as industry,
            patch.object(admuse_download, "set_search_keyword", return_value=True) as keyword,
            patch.object(admuse_download, "set_sort_order", return_value=True) as sort_order,
            patch.object(admuse_download, "sync_session_cookies", return_value=6) as cookies,
        ):
            recovered = admuse_download.recover_admuse_collection_session(
                page,
                context,
                session,
                "金融",
                "保险服务",
                "近7天",
                "快手",
                "曝光量降序",
                "测试掉线",
            )

        self.assertTrue(recovered)
        self.assertEqual(2, ensure.call_count)
        self.assertEqual(admuse_download.ADMUSE_IDEA_URL, page.goto_calls[0][0])
        image_tab.assert_called_once_with(page)
        time_range.assert_called_once_with(page, "近7天")
        industry.assert_called_once_with(page, "金融", "保险服务")
        keyword.assert_called_once_with(page, "快手")
        sort_order.assert_called_once_with(page, "曝光量降序")
        cookies.assert_called_once_with(context, session)


class TimeRangeTests(unittest.TestCase):
    def test_current_time_range_is_idempotent(self):
        page = unittest.mock.Mock()
        with (
            patch.object(admuse_download, "dismiss_admuse_blocking_overlays") as dismiss,
            patch.object(admuse_download, "get_current_time_range", return_value="近7天"),
        ):
            result = admuse_download.set_time_range(page, "近7天")

        self.assertTrue(result)
        dismiss.assert_called_once_with(page)
        page.evaluate.assert_not_called()

    def test_time_range_uses_open_visible_dropdown_and_verifies_value(self):
        page = unittest.mock.Mock()
        page.evaluate.side_effect = ([120, 80], True)
        with (
            patch.object(admuse_download, "dismiss_admuse_blocking_overlays"),
            patch.object(admuse_download, "get_current_time_range", side_effect=("近7天", "近3天")),
            patch.object(admuse_download.time, "sleep"),
        ):
            result = admuse_download.set_time_range(page, "近3天", max_attempts=1)

        self.assertTrue(result)
        selector_script = page.evaluate.call_args_list[0].args[0]
        self.assertIn("new Set(['近1天', '近3天', '近7天', '近30天'])", selector_script)
        self.assertIn("timeOptions.has(text)", selector_script)
        self.assertNotIn("text.includes('全部')", selector_script)
        dropdown_script = page.evaluate.call_args_list[1].args[0]
        self.assertIn("querySelectorAll('.selection-drop')", dropdown_script)
        self.assertIn("candidate.offsetParent !== null", dropdown_script)


class SortOrderTests(unittest.TestCase):
    def test_all_live_sort_options_are_supported(self):
        self.assertEqual(
            (
                "跑量得分降序", "曝光量降序", "点击率降序",
                "曝光转化率降序", "点击转化率降序", "综合得分降序",
            ),
            admuse_download.ADMUSE_SORT_OPTIONS,
        )

    def test_current_sort_is_idempotent(self):
        page = unittest.mock.Mock()
        with (
            patch.object(admuse_download, "dismiss_admuse_blocking_overlays") as dismiss,
            patch.object(admuse_download, "get_current_sort_order", return_value="点击率降序"),
        ):
            result = admuse_download.set_sort_order(page, "点击率降序")

        self.assertTrue(result)
        dismiss.assert_called_once_with(page)
        page.mouse.click.assert_not_called()

    def test_sort_uses_visible_dropdown_and_verifies_value(self):
        page = unittest.mock.Mock()
        page.evaluate.side_effect = ([150, 90], True)
        with (
            patch.object(admuse_download, "dismiss_admuse_blocking_overlays"),
            patch.object(admuse_download, "get_current_sort_order", side_effect=("跑量得分降序", "综合得分降序")),
            patch.object(admuse_download.time, "sleep"),
        ):
            result = admuse_download.set_sort_order(page, "综合得分降序", max_attempts=1)

        self.assertTrue(result)
        dropdown_script = page.evaluate.call_args_list[1].args[0]
        self.assertIn(".selection-drop li", dropdown_script)
        self.assertIn("item.offsetParent === null", dropdown_script)


class CdpGuardTests(unittest.TestCase):
    def test_localhost_fallback_is_tried_after_explicit_loopback(self):
        original_endpoint = cdp_guard.CDP_ENDPOINT
        try:
            cdp_guard.CDP_ENDPOINT = "http://127.0.0.1:9223"
            with patch.object(
                cdp_guard,
                "_probe_endpoint",
                side_effect=(
                    {"ready": False, "endpoint": "http://127.0.0.1:9223", "error": "refused"},
                    {"ready": True, "endpoint": "http://localhost:9223", "websocket": "ws://localhost:9223/devtools/browser/1"},
                ),
            ) as probe:
                result = cdp_guard.probe_cdp()
        finally:
            cdp_guard.CDP_ENDPOINT = original_endpoint

        self.assertTrue(result["ready"])
        self.assertEqual("http://localhost:9223", result["endpoint"])
        self.assertEqual(
            ["http://127.0.0.1:9223", "http://localhost:9223"],
            [call.args[0] for call in probe.call_args_list],
        )

    def test_auto_restart_targets_account_manager_main_pid(self):
        with (
            patch.object(cdp_guard, "probe_cdp", return_value={"ready": False, "error": "refused"}),
            patch.object(cdp_guard, "find_onion", return_value=r"C:\\onion.exe"),
            patch.object(cdp_guard, "_running_onion_path", return_value=r"C:\\onion.exe"),
            patch.object(
                cdp_guard,
                "_running_onion_info",
                return_value={"ProcessId": 1234, "ExecutablePath": r"C:\\onion.exe", "CommandLine": '"C:\\onion.exe"'},
            ),
            patch.object(cdp_guard, "_wait_ready", side_effect=[False, True]),
            patch.object(cdp_guard, "_launch", return_value=True) as launch,
            patch.object(cdp_guard.subprocess, "run") as run,
        ):
            self.assertTrue(cdp_guard.ensure_cdp())

        run.assert_called_once_with(
            ["taskkill", "/F", "/PID", "1234", "/T"],
            capture_output=True,
            timeout=10,
        )
        launch.assert_called_once_with(r"C:\\onion.exe", "--remote-debugging-port=9223")

    def test_no_restart_when_account_manager_is_already_running(self):
        with (
            patch.object(cdp_guard, "cdp_ready", return_value=False),
            patch.object(cdp_guard, "probe_cdp", return_value={"ready": False, "error": "refused"}),
            patch.object(cdp_guard, "find_onion", return_value=r"C:\\onion.exe"),
            patch.object(cdp_guard, "_running_onion_path", return_value=r"C:\\onion.exe"),
            patch.object(cdp_guard, "_wait_ready", return_value=False),
            patch.object(cdp_guard, "_launch") as launch,
            patch.object(cdp_guard.subprocess, "run") as run,
        ):
            self.assertFalse(cdp_guard.ensure_cdp(allow_restart=False))

        run.assert_not_called()
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class AdmuseCredentialLookupTests(unittest.TestCase):
    """凭据文件定位。

    踩过的坑：原来写死「往上三级 + data/shared」，独立版把 data/shared 放在
    **兄弟工程**（sibling_workspace_a / sibling_workspace_b）里，
    路径算出来是 `C:/Users/admin/Videos/data/...`，永远不存在 ——
    表现就是「妙思掉线后不会自动补登录」，而且失败还是静默的。
    """

    def test_local_override_has_highest_priority(self):
        candidates = admuse_download.admuse_qq_credentials_candidates()
        self.assertEqual(
            os.path.abspath(admuse_download.ADMUSE_QQ_CREDENTIALS_LOCAL),
            candidates[0],
        )

    def test_candidates_cover_sibling_workspaces(self):
        candidates = admuse_download.admuse_qq_credentials_candidates()
        parent = os.path.dirname(admuse_download.APP_ROOT)
        for name in admuse_download.ADMUSE_QQ_SIBLING_WORKSPACES:
            expected = os.path.abspath(
                os.path.join(parent, name, admuse_download.ADMUSE_QQ_CREDENTIALS_RELATIVE)
            )
            self.assertIn(expected, candidates)

    def test_candidates_are_deduplicated(self):
        candidates = admuse_download.admuse_qq_credentials_candidates()
        self.assertEqual(len(candidates), len(set(candidates)))

    def test_candidates_walk_up_from_the_script(self):
        candidates = admuse_download.admuse_qq_credentials_candidates()
        script_dir = os.path.dirname(os.path.abspath(admuse_download.__file__))
        walked = os.path.abspath(
            os.path.join(script_dir, admuse_download.ADMUSE_QQ_CREDENTIALS_RELATIVE)
        )
        self.assertIn(walked, candidates)

    def test_resolver_returns_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cred.txt"
            target.write_text("123456789\npassword\n", encoding="utf-8")
            with patch.object(
                admuse_download,
                "admuse_qq_credentials_candidates",
                return_value=[str(target)],
            ):
                self.assertEqual(str(target), admuse_download.resolve_admuse_qq_credentials_file())

    def test_resolver_returns_none_when_nothing_exists(self):
        with patch.object(
            admuse_download,
            "admuse_qq_credentials_candidates",
            return_value=["C:/definitely/not/here/cred.txt"],
        ):
            self.assertIsNone(admuse_download.resolve_admuse_qq_credentials_file())

    def test_loads_two_unlabeled_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cred.txt"
            target.write_text("123456789\npassword12\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"ADMUSE_QQ_CREDENTIALS_FILE": str(target)},
                clear=False,
            ):
                for key in ("ADMUSE_QQ_USERNAME", "ADMUSE_QQ_PASSWORD"):
                    os.environ.pop(key, None)
                username, password, source = admuse_download.load_admuse_qq_credentials()
        self.assertEqual("123456789", username)
        self.assertEqual("password12", password)
        self.assertEqual("file", source)

    def test_environment_wins_over_file(self):
        with patch.dict(
            os.environ,
            {"ADMUSE_QQ_USERNAME": "envuser", "ADMUSE_QQ_PASSWORD": "envpass"},
            clear=False,
        ):
            username, password, source = admuse_download.load_admuse_qq_credentials()
        self.assertEqual(("envuser", "envpass", "environment"), (username, password, source))


class AdmuseMissingCredentialIsLoudTests(unittest.TestCase):
    """凭据缺失必须说出来，不能静默跳过自动登录。"""

    def test_missing_credentials_prints_a_reason(self):
        class FakePage:
            url = "https://admuse.qq.com/#/idea"
            context = None

        with patch.object(
            admuse_download, "load_admuse_qq_credentials", return_value=("", "", None)
        ):
            import io
            from contextlib import redirect_stdout

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                result = admuse_download.attempt_admuse_qq_password_login(FakePage())

        self.assertFalse(result)
        output = buffer.getvalue()
        self.assertIn("LOGIN_STATUS|NO_CREDENTIAL|妙思", output)
        self.assertIn("ADMUSE_QQ_CREDENTIALS_FILE", output)

    def test_reason_does_not_leak_credentials(self):
        class FakePage:
            url = "https://admuse.qq.com/#/idea"
            context = None

        with patch.object(
            admuse_download,
            "load_admuse_qq_credentials",
            return_value=("", "", None),
        ):
            import io
            from contextlib import redirect_stdout

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                admuse_download.attempt_admuse_qq_password_login(FakePage())
        self.assertNotIn("password", buffer.getvalue().lower().replace("密码", ""))
