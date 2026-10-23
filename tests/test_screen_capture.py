import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import numpy as np
from PIL import Image
import Quartz
from Foundation import NSURL

from geometry import find_main_window
from screen_capture import (WindowCaptureError, BlankWindowError, _await_callback, _image_rgb,
                            capture_window, capture_window_rgb, _visible_target, _visible_display)


def window(owner="微信", title="微信", width=900, height=650, number=1):
    return {"kCGWindowOwnerName": owner, "kCGWindowName": title,
            "kCGWindowNumber": number, "kCGWindowLayer": 0,
            "kCGWindowBounds": {"X": 0, "Y": 0, "Width": width, "Height": height}}


class WindowCaptureTests(unittest.TestCase):
    def test_blank_window_layer_falls_back_to_visible_region(self):
        rgb = np.ones((60, 100, 3), dtype=np.uint8)
        with patch("screen_capture.check_screen_permission"), patch(
                "screen_capture._modern_backend", return_value=object()), patch(
                "screen_capture._visible_capture_windows", set()), patch(
                "screen_capture._modern_image") as capture, patch(
                "screen_capture._image_rgb", side_effect=[BlankWindowError("blank layer"), rgb]):
            self.assertIs(capture_window_rgb(111), rgb)
            self.assertTrue(capture.call_args.kwargs["visible"])

    def test_visible_capture_requires_target_app_in_front(self):
        target = window(number=11)
        target["kCGWindowOwnerPID"] = 77
        with patch("screen_capture.Quartz.CGWindowListCopyWindowInfo", return_value=[target]), patch(
                "AppKit.NSWorkspace") as workspace:
            workspace.sharedWorkspace.return_value.frontmostApplication.return_value.processIdentifier.return_value = 88
            with self.assertRaisesRegex(WindowCaptureError, "最前面"):
                _visible_target(11)

    def test_visible_capture_rejects_other_app_covering_target(self):
        target, cover = window(number=11), window(owner="Other App", number=12)
        target["kCGWindowOwnerPID"], cover["kCGWindowOwnerPID"] = 77, 88
        with patch("screen_capture.Quartz.CGWindowListCopyWindowInfo", return_value=[cover, target]), patch(
                "AppKit.NSWorkspace") as workspace:
            workspace.sharedWorkspace.return_value.frontmostApplication.return_value.processIdentifier.return_value = 77
            with self.assertRaisesRegex(WindowCaptureError, "遮挡"):
                _visible_target(11)

    def test_visible_capture_rejects_target_moved_during_capture(self):
        target = window(number=11)
        target["kCGWindowOwnerPID"] = 77
        with patch("screen_capture.Quartz.CGWindowListCopyWindowInfo", return_value=[target]), patch(
                "AppKit.NSWorkspace") as workspace:
            workspace.sharedWorkspace.return_value.frontmostApplication.return_value.processIdentifier.return_value = 77
            with self.assertRaisesRegex(WindowCaptureError, "位置发生变化"):
                _visible_target(11, expected=Quartz.CGRectMake(10, 0, 900, 650))

    def test_visible_capture_requires_entire_window_on_one_display(self):
        display = Mock()
        display.frame.return_value = Quartz.CGRectMake(0, 0, 1470, 956)
        with self.assertRaisesRegex(WindowCaptureError, "超出屏幕"):
            _visible_display([display], Quartz.CGRectMake(270, 300, 1200, 740))
        self.assertIs(_visible_display([display], Quartz.CGRectMake(100, 100, 900, 650)), display)

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
        with patch("screen_capture.Quartz.CGPreflightScreenCaptureAccess", return_value=True), patch(
                "screen_capture._modern_backend", return_value=None), patch(
                "screen_capture.Quartz.CGWindowListCreateImage", return_value=None):
            with self.assertRaisesRegex(WindowCaptureError, "微信窗口图像") as raised:
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
