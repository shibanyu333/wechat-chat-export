#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定位微信主窗口 + 聊天消息滚动区。
输出的坐标全部是【屏幕点坐标(pt)】，供滚动事件使用；
以及【窗口截图像素坐标(px)】，供裁剪使用。scale = px/pt。
"""
import os
import shutil
import subprocess
import tempfile
import time
import numpy as np
from PIL import Image
import Quartz

# 微信主窗口的标题(子窗如「图片和视频」「XX的聊天记录」不在此列)
MAIN_TITLES = ("微信", "WeChat", "Weixin", "微信 (测试版)")


def find_main_window():
    wins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID)
    cands = []
    for w in wins:
        owner = w.get("kCGWindowOwnerName", "")
        # 子串匹配也会匹配本工具「微信聊天记录导出」，从而抓到自己的界面。
        if owner in ("WeChat", "微信", "Weixin") and w.get("kCGWindowLayer", 0) == 0:
            b = w["kCGWindowBounds"]
            cands.append({
                "id": w["kCGWindowNumber"],
                "name": w.get("kCGWindowName", ""),
                "x": float(b["X"]), "y": float(b["Y"]),
                "w": float(b["Width"]), "h": float(b["Height"]),
            })
    if not cands:
        raise RuntimeError("未找到微信窗口，请确认微信主窗口已打开（未最小化）")
    # 主窗标题固定是「微信 / WeChat」；图片预览窗、"XX的聊天记录"搜索窗等子窗
    # 有各自标题，而且常常比主窗更大(实测 图片和视频 825x798 > 主窗 882x640)，
    # 只按面积挑会挑到子窗、抓出一堆废图。所以先按标题锁定主窗。
    main = [c for c in cands if c["name"].strip() in MAIN_TITLES]
    if not main:
        raise RuntimeError("未找到微信聊天主窗口，请打开微信中的目标会话。")
    win = max(main, key=lambda w: w["w"] * w["h"])
    if win["w"] < 450 or win["h"] < 400:
        raise RuntimeError("当前是微信登录或验证窗口，请登录后打开目标会话。")
    return win


def has_visible_content(rgb):
    """只拒绝整张纯色帧；不根据窗口共享标志或其他应用的位置推断截图结果。"""
    if rgb.ndim != 3 or rgb.shape[2] < 3 or not rgb.size:
        return False
    return bool(np.any(np.ptp(rgb[:, :, :3], axis=(0, 1)) > 0))


def capture_window(win_id, out_path):
    # 截图子进程只写系统临时目录，避免其文稿访问授权与主进程缓存不一致。
    # 验证图像后由主进程保存；取图失败与保存失败必须分别报告。
    folder = os.path.dirname(os.path.abspath(out_path))
    # screencapture 对以点开头的隐藏目标会返回0但拒绝写入，因此这里只能用普通文件名。
    fd, temporary = tempfile.mkstemp(prefix="wechat-window-", suffix=".png")
    os.close(fd)
    last_error = "截图没有返回有效图像"
    try:
        for attempt in range(3):
            if attempt:
                time.sleep(0.2)
            if os.path.exists(temporary):
                os.remove(temporary)
            try:
                result = subprocess.run(
                    ["/usr/sbin/screencapture", "-x", "-o", "-l", str(win_id), temporary],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=8,
                    cwd=os.path.dirname(temporary))
            except subprocess.TimeoutExpired:
                last_error = "系统截图命令超时"
                continue
            detail = result.stderr or b""
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
            if result.returncode != 0:
                last_error = detail.strip()[-300:] or f"系统截图命令退出码 {result.returncode}"
                continue
            try:
                with Image.open(temporary) as image:
                    if "A" in image.getbands() and image.getchannel("A").getextrema()[1] == 0:
                        last_error = "窗口返回了全透明图像"
                        continue
                    if not has_visible_content(np.asarray(image.convert("RGB"))):
                        last_error = "窗口返回了纯色图像"
                        continue
            except OSError as error:
                last_error = detail.strip()[-300:] or "系统截图文件无法读取：" + str(error)
                continue
            output_staging = None
            try:
                fd, output_staging = tempfile.mkstemp(prefix=".window-", suffix=".png", dir=folder)
                with os.fdopen(fd, "wb") as destination, open(temporary, "rb") as source:
                    shutil.copyfileobj(source, destination)
                os.replace(output_staging, out_path)
            except OSError as error:
                raise RuntimeError("截图已取得，但无法保存到工作目录：" + str(error)) from error
            finally:
                if output_staging and os.path.exists(output_staging):
                    os.remove(output_staging)
            return out_path
        if not Quartz.CGPreflightScreenCaptureAccess():
            raise RuntimeError("此版本尚未获得屏幕录制权限，无法截取聊天窗口。")
        raise RuntimeError(
            "未取得微信聊天画面，已停止抓取。当前录屏权限正常，无需反复修改权限。"
            "已保存的截图仍可通过「恢复上次抓取」导出。具体原因：" + last_error)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def detect_regions(png_path, win):
    """在窗口截图上检测：列表|聊天区分界线、消息区上下边界。返回像素坐标 + scale。"""
    im = Image.open(png_path).convert("L")
    a = np.asarray(im, dtype=np.float32)
    H, W = a.shape
    scale = W / win["w"]

    # ---- 左边界：列表列 与 聊天区 的竖直分界 ----
    # 每列中位亮度，在左 55% 宽度内找最强"台阶"(相邻列中位亮度突变)。
    col_med = np.median(a, axis=0)
    # 平滑
    k = 5
    col_s = np.convolve(col_med, np.ones(k) / k, mode="same")
    left_lo = int(W * 0.20)   # 跳过图标栏
    left_hi = int(W * 0.55)
    diffs = np.abs(np.diff(col_s))
    # 在候选范围里找最大跳变
    seg = diffs[left_lo:left_hi]
    pane_x = left_lo + int(np.argmax(seg)) + 1

    # ---- 上下边界：只在聊天区列范围内算逐行方差 ----
    pane = a[:, pane_x:]
    row_std = pane.std(axis=1)
    thr = row_std.max() * 0.12  # 内容行阈值
    content_rows = row_std > thr

    # 顶部：从上往下跳过标题栏(前若干行有标题文字也算内容)，
    #       取"标题带"之后第一段稳定内容的起点。简化：标题栏高度约 64pt。
    header_px = int(64 * scale)
    top = header_px
    # 微调：从 header_px 向下找第一行内容
    for y in range(header_px, H // 2):
        if content_rows[y]:
            top = y
            break

    # 底部：输入框是大块低方差区。从底向上找"低方差带"(输入框)的顶。
    # 先跳过底部工具栏(有图标,高方差) ~ 60pt
    toolbar_px = int(58 * scale)
    y = H - toolbar_px
    # 进入输入框(低方差)
    while y > H // 2 and content_rows[y]:
        y -= 1
    # 穿过输入框低方差带，直到重新遇到内容(消息)
    while y > H // 2 and not content_rows[y]:
        y -= 1
    bottom = y

    # 兜底：消息区不可能只占窗口的一小条。实测抓在微信重绘中间的帧上时，
    # 逐行方差会把输入框上方的空白带算错，返回过 702px(正常 1197px)的高度；
    # 几何只在开抓时测一次，一旦测歪，整段抓取都会按错的下边界裁，内容直接丢。
    # 所以低于窗口一半就不信它，退回"窗口高 - 底部工具栏 - 一屏典型输入框"。
    if bottom - top < H * 0.45:
        bottom = max(top + int(H * 0.45), H - int(150 * scale))

    return {
        "scale": scale,
        "pane_x_px": pane_x,
        "top_px": top,
        "bottom_px": bottom,
        "W": W, "H": H,
    }


if __name__ == "__main__":
    win = find_main_window()
    print("主窗口:", win)
    png = capture_window(win["id"], "recon_main_window.png")
    reg = detect_regions(png, win)
    print("检测结果(px):", reg)
    # 裁出消息区给人眼确认
    im = Image.open(png)
    crop = im.crop((reg["pane_x_px"], reg["top_px"], reg["W"], reg["bottom_px"]))
    crop.save("recon_msgarea.png")
    print("已裁出消息区 -> recon_msgarea.png  尺寸:", crop.size)
    # 换算成点坐标(相对窗口)供滚动使用
    s = reg["scale"]
    print("消息区(相对窗口, pt): x0=%.0f top=%.0f bottom=%.0f 宽=%.0f" % (
        reg["pane_x_px"]/s, reg["top_px"]/s, reg["bottom_px"]/s, (reg["W"]-reg["pane_x_px"])/s))
