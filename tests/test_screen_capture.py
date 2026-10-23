import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import numpy as np
from PIL import Image
import Quartz
from Foundation import NSURL

from geometry import find_main_window, detect_regions
from screen_capture import (WindowCaptureError, BlankWindowError, _await_callback, _image_rgb,
                            capture_window, capture_window_rgb)


def window(owner="微信", title="微信", width=900, height=650, number=1):
    return {"kCGWindowOwnerName": owner, "kCGWindowName": title,
            "kCGWindowNumber": number, "kCGWindowLayer": 0,
            "kCGWindowBounds": {"X": 0, "Y": 0, "Width": width, "Height": height}}


class WindowCaptureTests(unittest.TestCase):
    def test_protected_window_never_falls_back_to_desktop(self):
        with patch("screen_capture.check_screen_permission"), patch(
                "screen_capture._window_info", return_value={"kCGWindowSharingState": 0}), patch(
                "screen_capture.Quartz.CGWindowListCreateImage") as legacy, patch(
                "screen_capture._modern_image") as modern:
            with self.assertRaisesRegex(WindowCaptureError, "独立窗口"):
                capture_window_rgb(111)
            legacy.assert_not_called()
            modern.assert_not_called()

    def test_working_original_window_capture_is_used_first(self):
        rgb = np.ones((60, 100, 3), dtype=np.uint8)
        with patch("screen_capture.check_screen_permission"), patch(
                "screen_capture._window_info", return_value={"kCGWindowSharingState": 1}), patch(
                "screen_capture.Quartz.CGWindowListCreateImage", return_value=object()), patch(
                "screen_capture._image_rgb", return_value=rgb), patch(
                "screen_capture._modern_image") as modern:
            self.assertIs(capture_window_rgb(111), rgb)
            modern.assert_not_called()

    def test_blank_original_image_retries_same_window_only(self):
        rgb = np.ones((60, 100, 3), dtype=np.uint8)
        with patch("screen_capture.check_screen_permission"), patch(
                "screen_capture._window_info", return_value={"kCGWindowSharingState": 1}), patch(
                "screen_capture.Quartz.CGWindowListCreateImage", return_value=object()), patch(
                "screen_capture._modern_backend", return_value="backend"), patch(
                "screen_capture._modern_image") as modern, patch(
                "screen_capture._image_rgb", side_effect=[BlankWindowError("blank"), rgb]):
            self.assertIs(capture_window_rgb(111), rgb)
            modern.assert_called_once_with("backend", 111)

    def test_foreground_independent_chat_is_selected(self):
        with patch("geometry.Quartz.CGWindowListCopyWindowInfo", return_value=[
                window(title="测试会话", width=460, number=12), window(number=11)]):
            selected = find_main_window()
            self.assertEqual(selected["id"], 12)
            self.assertTrue(selected["standalone"])

    def test_does_not_switch_to_a_chat_behind_the_main_window(self):
        main = window(number=11)
        main["kCGWindowSharingState"] = 0
        with patch("geometry.Quartz.CGWindowListCopyWindowInfo", return_value=[
                main, window(title="另一会话", number=12)]):
            self.assertEqual(find_main_window()["id"], 11)

    def test_selected_window_is_retained_when_z_order_changes(self):
        with patch("geometry.Quartz.CGWindowListCopyWindowInfo", return_value=[
                window(number=11), window(title="测试会话", number=12)]):
            self.assertEqual(find_main_window(preferred_id=12)["id"], 12)
        with patch("geometry.Quartz.CGWindowListCopyWindowInfo", return_value=[window(number=11)]):
            with self.assertRaisesRegex(RuntimeError, "已关闭"):
                find_main_window(preferred_id=12)

    def test_activating_wechat_does_not_reopen_its_main_window(self):
        from wechat_ui import ensure_front
        app = Mock()
        with patch("wechat_ui.AppKit.NSRunningApplication") as running, patch(
                "wechat_ui.frontmost_name", return_value="微信"), patch(
                "wechat_ui.time.sleep"), patch("wechat_ui.subprocess.run") as reopen:
            running.runningApplicationsWithBundleIdentifier_.return_value = [app]
            self.assertTrue(ensure_front())
            app.activateWithOptions_.assert_called_once()
            reopen.assert_not_called()

    def test_independent_chat_does_not_crop_away_left_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            pixels = np.full((900, 600, 3), 31, dtype=np.uint8)
            pixels[150:210, 60:350] = 80
            path = Path(temp) / "independent.png"
            Image.fromarray(pixels).save(path)
            regions = detect_regions(str(path), {"w": 300, "standalone": True})
            self.assertEqual(regions["pane_x_px"], 0)
            self.assertEqual(regions["scale"], 2.0)

    def test_login_window_is_not_a_chat_window(self):
        with patch("geometry.Quartz.CGWindowListCopyWindowInfo", return_value=[
                window(width=280, height=380)]):
            with self.assertRaisesRegex(RuntimeError, "完成登录"):
                find_main_window()

    def test_exporter_is_not_mistaken_for_wechat(self):
        with patch("geometry.Quartz.CGWindowListCopyWindowInfo", return_value=[
                window(owner="微信聊天记录导出")]):
            with self.assertRaisesRegex(RuntimeError, "未找到微信窗口"):
                find_main_window()

    def test_main_chat_wins_over_larger_preview(self):
        with patch("geometry.Quartz.CGWindowListCopyWindowInfo", return_value=[
                window(number=11), window(title="图片和视频", width=1400, number=12)]):
            self.assertEqual(find_main_window()["id"], 11)

    def test_missing_window_is_not_reported_as_missing_permission(self):
        with patch("screen_capture._window_info", return_value={"kCGWindowSharingState": 1}), patch(
                "screen_capture.Quartz.CGPreflightScreenCaptureAccess", return_value=True), patch(
                "screen_capture._modern_backend", return_value=None), patch(
                "screen_capture.Quartz.CGWindowListCreateImage", return_value=None):
            with self.assertRaisesRegex(WindowCaptureError, "聊天窗口图像") as raised:
                capture_window_rgb(123)
        self.assertNotIn("权限", str(raised.exception))

    def test_denied_permission_is_reported_separately(self):
        with patch("screen_capture.Quartz.CGPreflightScreenCaptureAccess", return_value=False):
            with self.assertRaisesRegex(WindowCaptureError, "屏幕录制"):
                capture_window_rgb(123)

    def test_callback_error_preserves_actual_reason(self):
        with self.assertRaisesRegex(WindowCaptureError, "window disappeared"):
            _await_callback(lambda cb: cb(None, "window disappeared"), "截图")

    def test_callback_timeout_does_not_hang_capture(self):
        with self.assertRaisesRegex(WindowCaptureError, "超时"):
            _await_callback(lambda cb: None, "截图", timeout=0)

    def test_rgba_conversion_keeps_colours_and_orientation_and_rejects_blank(self):
        with tempfile.TemporaryDirectory() as temp:
            for blank in (False, True):
                pixels = np.zeros((64, 64, 3), dtype=np.uint8)
                if not blank:
                    pixels[:32] = (250, 10, 20)
                    pixels[32:] = (20, 30, 240)
                path = Path(temp) / "fixture.png"
                Image.fromarray(pixels).save(path)
                source = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(path)), None)
                cg_image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
                if blank:
                    with self.assertRaisesRegex(WindowCaptureError, "空白画面"):
                        _image_rgb(cg_image)
                else:
                    np.testing.assert_array_equal(_image_rgb(cg_image), pixels)

    def test_save_failure_is_not_reported_as_missing_permission(self):
        with patch("screen_capture.capture_window_rgb", return_value=np.zeros((64, 64, 3), dtype=np.uint8)), patch(
                "PIL.Image.Image.save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(WindowCaptureError, "无法保存文件.*disk full"):
                capture_window(123, "/unused.png")


if __name__ == "__main__":
    unittest.main()
