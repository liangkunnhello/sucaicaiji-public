import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import internal_export


class PlatformPage:
    def __init__(self, url, selected=False):
        self.url = url
        self.selected = selected

    def evaluate(self, _script, _labels):
        return not self.selected


class InternalLoginTests(unittest.TestCase):
    def test_internal_filename_starts_with_consumption(self):
        self.assertEqual(
            "123.46-APP_旅游素材.jpg",
            internal_export.build_consumption_filename("APP:旅游素材", 123.456, "jpg"),
        )

    def test_internal_filename_keeps_unknown_consumption_explicit(self):
        self.assertEqual(
            "未知-APP_旅游素材.jpg",
            internal_export.build_consumption_filename("APP:旅游素材", None, "jpg"),
        )

    def test_only_material_consumption_page_uses_direct_export(self):
        self.assertFalse(
            internal_export.is_internal_material_url(
                "https://internal-center.example.com/login#/home"
            )
        )
        self.assertTrue(
            internal_export.is_internal_material_url(
                "https://internal-center.example.com/login#/home/video-material-consumption"
            )
        )

    def test_internal_home_page_can_use_direct_navigation(self):
        self.assertTrue(
            internal_export.can_use_internal_direct_target(
                {
                    "url": "https://internal-center.example.com/login#/home",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/page/1",
                }
            )
        )

    def test_unrelated_page_cannot_use_internal_direct_navigation(self):
        self.assertFalse(
            internal_export.can_use_internal_direct_target(
                {
                    "url": "https://admuse.qq.com/#/idea",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/page/2",
                }
            )
        )

    def test_home_hash_with_platform_cards_is_login_platform_page(self):
        page = PlatformPage("https://internal-center.example.com/login#/home")

        self.assertTrue(internal_export.is_yuntian_login_platform_page(page))

    def test_logged_in_material_page_is_not_platform_page(self):
        page = PlatformPage(
            "https://internal-center.example.com/login#/home/video-material-consumption",
            selected=True,
        )

        self.assertFalse(internal_export.is_yuntian_login_platform_page(page))

    def test_legacy_login_hash_is_still_supported(self):
        page = PlatformPage("https://internal-center.example.com/login#/login")

        self.assertTrue(internal_export.is_yuntian_login_platform_page(page))


if __name__ == "__main__":
    unittest.main()
