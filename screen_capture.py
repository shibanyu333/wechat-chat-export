"""窗口截图：新系统使用 ScreenCaptureKit，旧系统保留 Quartz。"""
import platform
import threading
import time

import numpy as np
import Quartz
from PIL import Image


class WindowCaptureError(RuntimeError):
    pass


class BlankWindowError(WindowCaptureError):
    pass


def check_screen_permission():
    if not Quartz.CGPreflightScreenCaptureAccess():
        raise WindowCaptureError(
            "缺少【屏幕录制】权限：请在系统设置 → 隐私与安全性 → 录屏与系统录音中"
            "开启「微信聊天记录导出」，然后退出并重新打开应用。")


def _modern_backend():
    version = platform.mac_ver()[0]
    if version and int(version.split('.')[0]) >= 14:
        try:
            import ScreenCaptureKit
        except ImportError as error:
            raise WindowCaptureError("截图组件缺失，请重新安装最新版导出工具。") from error
        return ScreenCaptureKit
    return None


def _await_callback(start, description, timeout=8):
    done = threading.Event()
    result = {}

    def completed(value, error):
        result.update(value=value, error=error)
        done.set()

    start(completed)
    if not done.wait(timeout):
        raise WindowCaptureError(description + "超时，请打开微信主窗口后重试。")
    if result['error'] is not None:
        raise WindowCaptureError(description + "失败：" + str(result['error']))
    if result['value'] is None:
        raise WindowCaptureError(description + "没有返回图像或窗口，请重新打开微信主窗口。")
    return result['value']


_content_cache = None
_content_time = 0.0


def _window_info(win_id):
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionIncludingWindow, win_id) or []
    return next((w for w in windows if w.get("kCGWindowNumber") == win_id), None)


def _modern_image(sc, win_id):
    """仅截取指定窗口，不使用整屏区域回退。"""
    import AppKit
    AppKit.NSApplication.sharedApplication()
    global _content_cache, _content_time
    now = time.monotonic()
    if _content_cache is None or now - _content_time > 1.0:
        _content_cache = _await_callback(
            lambda cb: sc.SCShareableContent.
            getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(True, True, cb),
            "读取可截图窗口")
        _content_time = now
    window = next((w for w in _content_cache.windows() if w.windowID() == win_id), None)
    if window is None:
        _content_cache = None
        raise WindowCaptureError("目标聊天窗口已关闭，请重新打开后再抓取。")
    content_filter = sc.SCContentFilter.alloc().initWithDesktopIndependentWindow_(window)
    scale = float(content_filter.pointPixelScale())
    frame = window.frame()
    config = sc.SCStreamConfiguration.alloc().init()
    config.setWidth_(max(1, round(frame.size.width * scale)))
    config.setHeight_(max(1, round(frame.size.height * scale)))
    config.setShowsCursor_(False)
    config.setCaptureResolution_(sc.SCCaptureResolutionBest)
    config.setIgnoreShadowsSingleWindow_(True)
    return _await_callback(
        lambda cb: sc.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
            content_filter, config, cb), "截取聊天窗口")


def _image_rgb(image):
    width, height = Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)
    if not width or not height:
        raise WindowCaptureError("微信窗口图像尺寸为空，请重新打开目标聊天。")
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    ctx = Quartz.CGBitmapContextCreate(
        rgba, width, height, 8, width * 4, Quartz.CGColorSpaceCreateDeviceRGB(),
        Quartz.kCGImageAlphaPremultipliedLast | Quartz.kCGBitmapByteOrder32Big)
    if ctx is None:
        raise WindowCaptureError("无法解码窗口截图，请重新打开导出工具。")
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, width, height), image)
    rgb = np.ascontiguousarray(rgba[:, :, :3])
    if float(rgb[::8, ::8].std(axis=(0, 1)).max()) < 1.0:
        raise BlankWindowError(
            "微信窗口返回空白画面，可能正在登录、切换窗口或已最小化。"
            "请打开已登录的聊天主窗口后重试；这不一定是录屏权限问题。")
    return rgb


def validate_window_capture(win_id):
    info = _window_info(win_id)
    if info is None:
        raise WindowCaptureError("目标聊天窗口已关闭，请重新打开后再抓取。")
    if info.get("kCGWindowSharingState") == 0:
        raise WindowCaptureError(
            "微信当前隐藏了这个窗口的截图。请双击要导出的会话，或右键选择“在独立窗口中打开”，"
            "将该聊天窗口放在微信其他窗口前面，再点击开始抓取。无需修改微信设置。")


def capture_window_rgb(win_id):
    check_screen_permission()
    validate_window_capture(win_id)
    # 优先恢复旧版的按窗口取图路径；失败时也只回退到同一个窗口。
    image = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow, win_id,
        Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageBestResolution)
    if image is not None:
        try:
            return _image_rgb(image)
        except BlankWindowError:
            pass
    sc = _modern_backend()
    if sc is not None:
        return _image_rgb(_modern_image(sc, win_id))
    raise WindowCaptureError("无法取得目标聊天窗口图像，请重新打开该聊天窗口后再抓取。")


def capture_window(win_id, out_path):
    rgb = capture_window_rgb(win_id)
    try:
        Image.fromarray(rgb).save(out_path)
    except OSError as error:
        raise WindowCaptureError("截图已取得，但无法保存文件：" + str(error)) from error
    return out_path
