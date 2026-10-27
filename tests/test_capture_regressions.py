"""Synthetic capture regressions: no live WeChat, desktop capture, or permission prompts."""
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import ApplicationServices as AX
import numpy as np
import Quartz
from PIL import Image

import engine
import geometry
import wechat_ui


def window(window_id=17, owner="WeChat", title="微信", width=900, height=700,
           layer=0, sharing=1):
    return {"kCGWindowNumber": window_id, "kCGWindowOwnerName": owner,
            "kCGWindowName": title, "kCGWindowLayer": layer,
            "kCGWindowSharingState": sharing,
            "kCGWindowBounds": {"X": 50, "Y": 30, "Width": width, "Height": height}}


def content_image():
    # Two distinct opaque regions exercise actual PNG decoding, without chat data.
    image = Image.new("RGB", (60, 40), (38, 38, 38))
    image.paste((220, 220, 220), (25, 8, 55, 32))
    return image


class WindowSelectionTests(unittest.TestCase):
    def test_main_window_wins_over_exporter_developer_preview_and_search(self):
        windows = [window(1, owner="微信聊天记录导出", width=1500, height=1000),
                   window(2, owner="微信开发者工具", width=1500, height=1000),
                   window(3, title="图片和视频", width=1500, height=1000),
                   window(4, title="某会话的聊天记录", width=1500, height=1000),
                   window(5, layer=3, width=1500, height=1000), window(17)]
        with patch.object(Quartz, "CGWindowListCopyWindowInfo", return_value=windows):
            self.assertEqual(geometry.find_main_window()["id"], 17)

    def test_unrelated_or_child_windows_are_not_used_when_main_is_absent(self):
        for candidate in (window(owner="微信聊天记录导出"),
                          window(owner="微信开发者工具"),
                          window(title="图片和视频"),
                          window(title="某会话的聊天记录"), window(layer=3)):
            with self.subTest(candidate=candidate), patch.object(
                    Quartz, "CGWindowListCopyWindowInfo", return_value=[candidate]):
                with self.assertRaises(RuntimeError):
                    geometry.find_main_window()

    def test_small_login_window_is_not_a_chat(self):
        with patch.object(Quartz, "CGWindowListCopyWindowInfo", return_value=[
                window(width=280, height=380)]):
            with self.assertRaisesRegex(RuntimeError, "登录|验证"):
                geometry.find_main_window()


class WindowCaptureTests(unittest.TestCase):
    def test_screenshot_destination_must_not_be_a_hidden_filename(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "frame.png"

            def capture(command, **kwargs):
                # macOS screencapture 对隐藏目标返回0但不生成文件，stderr才会报错。
                if Path(command[-1]).name.startswith("."):
                    return subprocess.CompletedProcess(command, 0, stderr=b"cannot write file to intended destination")
                content_image().save(command[-1])
                return subprocess.CompletedProcess(command, 0)

            with patch("geometry.subprocess.run", side_effect=capture), patch(
                    "geometry.time.sleep"), patch.object(
                    Quartz, "CGPreflightScreenCaptureAccess", return_value=True):
                geometry.capture_window(17, str(output))
            self.assertTrue(output.is_file())

    def test_screenshot_child_writes_outside_protected_documents_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            documents = Path(temp) / "Documents"
            documents.mkdir()
            output = documents / "frame.png"

            def capture(command, **kwargs):
                # 模拟当前主进程能写入文稿，但其截图子进程被文稿访问授权拒绝。
                child_cwd = Path(kwargs.get("cwd", documents))
                if documents in Path(command[-1]).parents or child_cwd == documents:
                    return subprocess.CompletedProcess(command, 1, stderr=b"Operation not permitted")
                content_image().save(command[-1])
                return subprocess.CompletedProcess(command, 0)

            with patch("geometry.subprocess.run", side_effect=capture), patch(
                    "geometry.time.sleep"), patch.object(
                    Quartz, "CGPreflightScreenCaptureAccess", return_value=True):
                geometry.capture_window(17, str(output))
            np.testing.assert_array_equal(np.asarray(Image.open(output)), np.asarray(content_image()))
            self.assertEqual(list(documents.glob(".window-*")), [])

    def test_output_save_failure_is_reported_separately_from_capture_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "frame.png"
            output.mkdir()  # 真实落盘失败：目标被同名目录占用。

            def capture(command, **kwargs):
                content_image().save(command[-1])
                return subprocess.CompletedProcess(command, 0)

            with patch("geometry.subprocess.run", side_effect=capture):
                with self.assertRaisesRegex(RuntimeError, "截图已取得.*保存"):
                    geometry.capture_window(17, str(output))
            self.assertTrue(output.is_dir())
            self.assertEqual(list(Path(temp).glob(".window-*")), [])

    def test_capture_failure_preserves_the_system_error(self):
        with tempfile.TemporaryDirectory() as temp, patch(
                "geometry.subprocess.run", return_value=subprocess.CompletedProcess(
                    [], 1, stderr=b"could not create image from window")), patch(
                "geometry.time.sleep"), patch.object(
                Quartz, "CGPreflightScreenCaptureAccess", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "could not create image from window"):
                geometry.capture_window(17, str(Path(temp) / "frame.png"))

    def test_sharing_none_still_attempts_original_window_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "frame.png"

            def capture(command, **kwargs):
                self.assertEqual(command[:-1], ["/usr/sbin/screencapture", "-x", "-o", "-l", "17"])
                content_image().save(command[-1])
                return subprocess.CompletedProcess(command, 0)

            with patch.object(Quartz, "CGWindowListCopyWindowInfo", return_value=[
                    window(sharing=0)]), patch("geometry.subprocess.run", side_effect=capture) as run:
                selected = geometry.find_main_window()
                self.assertEqual(geometry.capture_window(selected["id"], str(output)), str(output))
            run.assert_called_once()
            np.testing.assert_array_equal(np.asarray(Image.open(output)), np.asarray(content_image()))
            self.assertEqual(list(Path(temp).glob(".window-*")), [])

    def test_blank_first_frame_retries_same_window_and_keeps_real_second_frame(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "frame.png"
            attempts = []

            def capture(command, **kwargs):
                attempts.append(command)
                image = Image.new("RGB", (60, 40), "black") if len(attempts) == 1 else content_image()
                image.save(command[-1])
                return subprocess.CompletedProcess(command, 0)

            with patch("geometry.subprocess.run", side_effect=capture), patch("geometry.time.sleep"):
                geometry.capture_window(17, str(output))
            self.assertEqual(len(attempts), 2)
            self.assertTrue(all(command[:-1] == ["/usr/sbin/screencapture", "-x", "-o", "-l", "17"]
                                for command in attempts))
            np.testing.assert_array_equal(np.asarray(Image.open(output)), np.asarray(content_image()))
            self.assertEqual(list(Path(temp).glob(".window-*")), [])

    def test_transparent_png_with_varying_hidden_rgb_is_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "frame.png"
            transparent = content_image().convert("RGBA")
            transparent.putalpha(0)
            attempts = []

            def capture(command, **kwargs):
                attempts.append(command)
                image = transparent if len(attempts) == 1 else content_image()
                image.save(command[-1])
                return subprocess.CompletedProcess(command, 0)

            with patch("geometry.subprocess.run", side_effect=capture), patch("geometry.time.sleep"):
                geometry.capture_window(17, str(output))
            self.assertEqual(len(attempts), 2)
            self.assertTrue(all(command[:-1] == ["/usr/sbin/screencapture", "-x", "-o", "-l", "17"]
                                for command in attempts))
            with Image.open(output) as image:
                self.assertEqual(image.mode, "RGB")
                np.testing.assert_array_equal(np.asarray(image), np.asarray(content_image()))
            self.assertEqual(list(Path(temp).glob(".window-*")), [])

    def test_failed_commands_do_not_read_old_output_or_misreport_permission(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "frame.png"
            content_image().save(output)
            old_bytes = output.read_bytes()
            failure = subprocess.CompletedProcess([], 1, stderr=b"could not create image from window")
            with patch("geometry.subprocess.run", return_value=failure) as run, patch(
                    "geometry.time.sleep"), patch.object(
                    Quartz, "CGPreflightScreenCaptureAccess", return_value=True), patch(
                    "geometry.Image.open", side_effect=AssertionError("must not read a failed capture")):
                with self.assertRaisesRegex(RuntimeError, "未取得微信聊天画面") as failure:
                    geometry.capture_window(17, str(output))
            self.assertEqual(run.call_count, 3)
            self.assertNotIn("尚未获得", str(failure.exception))
            self.assertNotIn("检查【屏幕录制】", str(failure.exception))
            self.assertEqual(output.read_bytes(), old_bytes)
            self.assertEqual(list(Path(temp).glob(".window-*")), [])
            for call in run.call_args_list:
                self.assertEqual(call.args[0][:-1], ["/usr/sbin/screencapture", "-x", "-o", "-l", "17"])
                self.assertNotEqual(call.args[0][-1], str(output))

    def test_missing_permission_is_reported_only_when_permission_query_confirms_it(self):
        with tempfile.TemporaryDirectory() as temp, patch(
                "geometry.subprocess.run", return_value=subprocess.CompletedProcess([], 1)), patch(
                "geometry.time.sleep"), patch.object(
                Quartz, "CGPreflightScreenCaptureAccess", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "尚未获得屏幕录制权限"):
                geometry.capture_window(17, str(Path(temp) / "frame.png"))
            self.assertEqual(list(Path(temp).iterdir()), [])


class PreflightTests(unittest.TestCase):
    def test_preflight_only_queries_permissions_without_prompting_or_capturing_desktop(self):
        for accessibility, screen, expected in ((True, True, True), (False, True, False),
                                                 (True, False, False)):
            with self.subTest(accessibility=accessibility, screen=screen), ExitStack() as stack:
                temp = stack.enter_context(tempfile.TemporaryDirectory())
                stack.enter_context(patch("engine.TMP_DIR", temp))
                stack.enter_context(patch("engine.subprocess.check_output", return_value=b"123\n"))
                run = stack.enter_context(patch("engine.subprocess.run"))
                stack.enter_context(patch.object(AX, "AXIsProcessTrusted", return_value=accessibility))
                ax_prompt = stack.enter_context(patch.object(AX, "AXIsProcessTrustedWithOptions"))
                stack.enter_context(patch.object(Quartz, "CGPreflightScreenCaptureAccess", return_value=screen))
                screen_prompt = stack.enter_context(patch.object(Quartz, "CGRequestScreenCaptureAccess"))
                screenshot = stack.enter_context(patch.object(Quartz, "CGWindowListCreateImage"))
                self.assertEqual(engine.preflight()[0], expected)
                run.assert_not_called()
                ax_prompt.assert_not_called()
                screen_prompt.assert_not_called()
                screenshot.assert_not_called()
                self.assertEqual(list(Path(temp).iterdir()), [])


class CoreGraphicsCaptureTests(unittest.TestCase):
    def fake_cg_image(self, stack, bgra, alpha_info=Quartz.kCGImageAlphaPremultipliedFirst,
                      byte_order=Quartz.kCGBitmapByteOrder32Little):
        height, width, _ = bgra.shape
        stack.enter_context(patch.object(Quartz, "CGWindowListCreateImage", return_value=object()))
        stack.enter_context(patch.object(Quartz, "CGImageGetHeight", return_value=height))
        stack.enter_context(patch.object(Quartz, "CGImageGetWidth", return_value=width))
        stack.enter_context(patch.object(Quartz, "CGImageGetBytesPerRow", return_value=width * 4))
        stack.enter_context(patch.object(Quartz, "CGImageGetDataProvider", return_value=object()))
        stack.enter_context(patch.object(Quartz, "CGDataProviderCopyData", return_value=bgra.tobytes()))
        # All metadata is synthetic; never inspect an actual CoreGraphics image.
        stack.enter_context(patch.object(Quartz, "CGImageGetAlphaInfo", return_value=alpha_info))
        stack.enter_context(patch.object(Quartz, "CGImageGetBitmapInfo", return_value=byte_order | alpha_info))

    def test_solid_coregraphics_frame_returns_none_and_uses_old_window_command(self):
        for shade in (0, 255, 38):
            with self.subTest(shade=shade), ExitStack() as stack:
                bgra = np.full((40, 60, 4), shade, dtype=np.uint8)
                bgra[:, :, 3] = 255
                self.fake_cg_image(stack, bgra)
                self.assertIsNone(wechat_ui.capture_window_rgb(17))
                temp = stack.enter_context(tempfile.TemporaryDirectory())
                stack.enter_context(patch("wechat_ui._TMP", str(Path(temp) / "frame.png")))
                stack.enter_context(patch("geometry.time.sleep"))

                def capture(command, **kwargs):
                    self.assertEqual(command[:-1], ["/usr/sbin/screencapture", "-x", "-o", "-l", "17"])
                    content_image().save(command[-1])
                    return subprocess.CompletedProcess(command, 0)

                run = stack.enter_context(patch("geometry.subprocess.run", side_effect=capture))
                view = object.__new__(wechat_ui.WeChatView)
                view.win = {"id": 17}
                np.testing.assert_array_equal(view._full_rgb(), np.asarray(content_image()))
                run.assert_called_once()

    def test_textured_coregraphics_frame_preserves_rgb_channels(self):
        rgb = np.asarray(content_image())
        bgra = np.empty((*rgb.shape[:2], 4), dtype=np.uint8)
        bgra[:, :, :3] = rgb[:, :, ::-1]
        bgra[5, 8, :3] = (60, 40, 20)
        bgra[:, :, 3] = 255
        expected = rgb.copy()
        expected[5, 8] = (20, 40, 60)
        with ExitStack() as stack:
            self.fake_cg_image(stack, bgra)
            np.testing.assert_array_equal(wechat_ui.capture_window_rgb(17), expected)

    def test_transparent_coregraphics_frame_with_varying_hidden_rgb_is_rejected(self):
        first = (Quartz.kCGImageAlphaPremultipliedFirst, Quartz.kCGImageAlphaFirst)
        last = (Quartz.kCGImageAlphaPremultipliedLast, Quartz.kCGImageAlphaLast)
        for alpha_info in first + last:
            for byte_order in (Quartz.kCGBitmapByteOrder32Little, Quartz.kCGBitmapByteOrder32Big):
                with self.subTest(alpha=alpha_info, order=byte_order), ExitStack() as stack:
                    pixels = np.full((40, 60, 4), 30, dtype=np.uint8)
                    pixels[5:20, 10:40] = 200
                    little = byte_order == Quartz.kCGBitmapByteOrder32Little
                    alpha_index = (3 if little else 0) if alpha_info in first else (0 if little else 3)
                    pixels[:, :, alpha_index] = 0
                    self.fake_cg_image(stack, pixels, alpha_info, byte_order)
                    self.assertIsNone(wechat_ui.capture_window_rgb(17))

    def test_none_skip_first_zero_padding_is_not_treated_as_transparency(self):
        rgb = np.asarray(content_image())
        bgrx = np.zeros((*rgb.shape[:2], 4), dtype=np.uint8)
        bgrx[:, :, :3] = rgb[:, :, ::-1]
        with ExitStack() as stack:
            self.fake_cg_image(stack, bgrx, Quartz.kCGImageAlphaNoneSkipFirst)
            np.testing.assert_array_equal(wechat_ui.capture_window_rgb(17), rgb)


if __name__ == "__main__":
    unittest.main()
