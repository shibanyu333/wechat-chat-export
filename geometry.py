#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定位微信主窗口 + 聊天消息滚动区。
输出的坐标全部是【屏幕点坐标(pt)】，供滚动事件使用；
以及【窗口截图像素坐标(px)】，供裁剪使用。scale = px/pt。
"""
import subprocess
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
        if "WeChat" in owner or "微信" in owner:
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
    return max(main or cands, key=lambda w: w["w"] * w["h"])


def capture_window(win_id, out_path):
    r = subprocess.run(["screencapture", "-x", "-o", "-l", str(win_id), out_path])
    if r.returncode != 0:
        raise RuntimeError("截图失败，检查【屏幕录制】权限")
    return out_path


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
