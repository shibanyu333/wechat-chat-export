import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image
import Quartz
from Foundation import NSURL

from geometry import find_main_window
from screen_capture import (WindowCaptureError, _await_callback, _image_rgb,
                            capture_window, capture_window_rgb)


def window(owner="微信", title="微信", width=900, height=650, number=1):
    return {"kCGWindowOwnerName": owner, "kCGWindowName": title,
            "kCGWindowNumber": number, "kCGWindowLayer": 0,
            "kCGWindowBounds": {"X": 0, "Y": 0, "Width": width, "Height": height}}


class WindowCaptureTests(unittest.TestCase):
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
