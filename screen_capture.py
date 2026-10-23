"""窗口截图：新系统使用 ScreenCaptureKit，旧系统保留 Quartz。"""
import platform
import threading
import time

import numpy as np
import Quartz
from PIL import Image


class WindowCaptureError(RuntimeError):
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


def _modern_image(sc, win_id):
    # CLI 没有 pywebview 的 NSApplication；先初始化，避免 CGS_REQUIRE_INIT 崩溃。
    # App 后台线程走到这里时，主线程已经启动了 NSApplication。
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
        raise WindowCaptureError("微信窗口已关闭或发生变化，请打开目标聊天后重新开始抓取。")
    frame = window.frame()
    display = next((d for d in _content_cache.displays()
                    if Quartz.CGRectContainsPoint(d.frame(), Quartz.CGPointMake(
                        frame.origin.x + frame.size.width / 2,
                        frame.origin.y + frame.size.height / 2))), None)
    if display is None:
        raise WindowCaptureError("微信窗口不在可截图的显示器上，请移回屏幕内后重试。")
    # 只包含目标窗口的图层，其他应用不会进入截图。
    content_filter = sc.SCContentFilter.alloc().initWithDisplay_includingWindows_(display, [window])
    scale = float(content_filter.pointPixelScale())
    config = sc.SCStreamConfiguration.alloc().init()
    config.setWidth_(max(1, round(frame.size.width * scale)))
    config.setHeight_(max(1, round(frame.size.height * scale)))
    config.setShowsCursor_(False)
    config.setCaptureResolution_(sc.SCCaptureResolutionBest)
    config.setSourceRect_(Quartz.CGRectMake(
        frame.origin.x - display.frame().origin.x,
        frame.origin.y - display.frame().origin.y,
        frame.size.width, frame.size.height))
    return _await_callback(
        lambda cb: sc.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
            content_filter, config, cb), "截取微信窗口")


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
        raise WindowCaptureError(
            "微信窗口返回空白画面，可能正在登录、切换窗口或已最小化。"
            "请打开已登录的聊天主窗口后重试；这不一定是录屏权限问题。")
    return rgb


def capture_window_rgb(win_id):
    check_screen_permission()
    sc = _modern_backend()
    if sc is not None:
        image = _modern_image(sc, win_id)
    else:
        image = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow, win_id,
            Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageBestResolution)
    if image is None:
        raise WindowCaptureError(
            "无法取得微信窗口图像，请确认微信已登录且主窗口未最小化，再重新开始抓取。")
    return _image_rgb(image)


def capture_window(win_id, out_path):
    rgb = capture_window_rgb(win_id)
    try:
        Image.fromarray(rgb).save(out_path)
    except OSError as error:
        raise WindowCaptureError("截图已取得，但无法保存文件：" + str(error)) from error
    return out_path
