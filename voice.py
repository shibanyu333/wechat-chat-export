#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语音消息自动转文字：右键语音气泡→确认菜单含"转文字"→点击→等待转写出现。
安全保护：只有当右键菜单里确实存在"转文字"项时才点击，误判不会误操作。"""
import os, re, time, subprocess
import numpy as np
import Quartz
from PIL import Image
from wechat_ui import move_mouse, check_stop, TMP_DIR, note_self_click
from geometry import capture_window
from ocr import ocr_image

# 语音时长用引号(秒)，视频/通话用冒号→用引号区分语音
DURATION_RE = re.compile(r'\d{1,2}\s*("|\'\'|″|”|’’|\')')
ZH_RE = re.compile(r'^(?:转文字|转成文字|转换为文字|转为文字|Convert to Text)$', re.I)
COMPLETED_RE = re.compile(r'收起.*文字|隐藏.*文字|展开.*文字|显示.*文字|复制(?:文字|文本)'
                          r'|重新.*转.*文字|再次.*转.*文字|重新转写|重新识别|Hide Text|Show Text|Copy Text|Retranscribe', re.I)
VOICE_LABEL_RE = re.compile(r'^(?:转文字|转成文字|转换为文字|转为文字|播放|暂停|收起|展开|转写中|转文字中|转换中)$')


def pending_voice_rows(lines, pixels):
    """只返回未显示转写正文的时长行；相邻的普通消息不算转写。"""
    from parse3 import build_masks, close_h, bubbles_from
    _, green, gray = build_masks(pixels)
    bubbles = (bubbles_from(close_h(green), 'me', 1500, 40, 24)
               + bubbles_from(close_h(gray), 'them', 1500, 40, 24))
    width = pixels.shape[1]
    pending, skipped = [], 0
    for duration in lines:
        if duration['w'] > width*0.35 or len(duration['text']) > 10 or not DURATION_RE.search(duration['text']):
            continue
        cx, cy = duration['x']+duration['w']/2, duration['y']+duration['h']/2
        bubble = next((b for b in bubbles if b['x0']-8 <= cx <= b['x1']+8
                       and b['y0']-6 <= cy <= b['y1']+6), None)
        converted = False
        if bubble:
            for row in lines:
                if row is duration or row['x'] < width*0.07 or row['x']+row['w'] > width*0.93:
                    continue  # 头像里的字不属于转写正文。
                x, y = row['x']+row['w']/2, row['y']+row['h']/2
                if not (bubble['x0']-8 <= x <= bubble['x1']+8 and bubble['y0']-6 <= y <= bubble['y1']+6):
                    continue
                text = row['text'].strip()
                if VOICE_LABEL_RE.fullmatch(text) or DURATION_RE.search(text):
                    continue
                if re.search(r'[\u3400-\u9fffA-Za-z0-9]', text):
                    converted = True
                    break
        if converted:
            skipped += 1
        else:
            pending.append(duration)
    return pending, skipped


def _click(gx, gy, button="left"):
    # 声明这是工具自己点的，别被「连点鼠标=停止」当成用户要中断
    if button == "left":
        note_self_click()
    move_mouse(gx, gy)
    if button == "right":
        d, u, b = (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp,
                   Quartz.kCGMouseButtonRight)
    else:
        d, u, b = (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp,
                   Quartz.kCGMouseButtonLeft)
    dn = Quartz.CGEventCreateMouseEvent(None, d, Quartz.CGPointMake(gx, gy), b)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, dn); time.sleep(0.08)
    up = Quartz.CGEventCreateMouseEvent(None, u, Quartz.CGPointMake(gx, gy), b)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up); time.sleep(0.05)


def _escape():
    for down in (True, False):
        k = Quartz.CGEventCreateKeyboardEvent(None, 53, down)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, k); time.sleep(0.03)


def find_candidates(v):
    """返回当前窗口里疑似语音气泡的全局点坐标列表。"""
    s = v.scale
    png = os.path.join(TMP_DIR, "_v_win.png")
    capture_window(v.win["id"], png)
    im = Image.open(png)
    crop = im.crop((v.reg["pane_x_px"], v.reg["top_px"], v.reg["W"], v.reg["bottom_px"]))
    tmp = os.path.join(TMP_DIR, "_v_area.png"); crop.save(tmp)
    lines, W, H = ocr_image(tmp)
    cands = []
    rows, v.voice_skipped = pending_voice_rows(lines, np.asarray(crop.convert('RGB')))
    for l in rows:
        if l["w"] > W * 0.35:       # 语音气泡窄
            continue
        # 时长行常被 OCR 读成 'v 36"' / '»)) 30"' 等带图标噪声，放宽长度到 10；
        # 误检也安全：transcribe_one 只在菜单确有「转文字」时才点击。
        if DURATION_RE.search(l["text"]) and len(l["text"]) <= 10:
            gx = v.win["x"] + (v.reg["pane_x_px"] + l["x"] + l["w"] / 2) / s
            gy = v.win["y"] + (v.reg["top_px"] + l["y"] + l["h"] / 2) / s
            cands.append((gx, gy))
    for f in (png, tmp):
        if os.path.exists(f):
            os.remove(f)
    # 从上到下
    cands.sort(key=lambda c: c[1])
    return cands


def transcribe_one(v, gx, gy, wait=1.8):
    """右键该点，若菜单含"转文字"则点击。返回是否点击了。"""
    s = v.scale
    _click(gx, gy, "right")
    time.sleep(0.5)
    full = os.path.join(TMP_DIR, "_v_full.png")
    subprocess.run(["screencapture", "-x", full],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    im = Image.open(full)
    cx0 = max(0, int(gx * s) - 40 * int(s)); cy0 = max(0, int(gy * s) - 20 * int(s))
    cx1 = min(im.width, cx0 + 400 * int(s)); cy1 = min(im.height, cy0 + 560 * int(s))
    menu = im.crop((cx0, cy0, cx1, cy1))
    mtmp = os.path.join(TMP_DIR, "_v_menu.png"); menu.save(mtmp)
    lines, _, _ = ocr_image(mtmp)
    for f in (full, mtmp):
        if os.path.exists(f):
            os.remove(f)
    if any(COMPLETED_RE.search(l['text']) for l in lines):
        _escape(); time.sleep(0.15)
        return False
    hit = next((l for l in lines if ZH_RE.fullmatch(l["text"].strip())), None)
    if not hit:
        _escape(); time.sleep(0.15)
        return False
    # 菜单项全局坐标
    tgx = (cx0 + hit["x"] + hit["w"] / 2) / s
    tgy = (cy0 + hit["y"] + hit["h"] / 2) / s
    _click(tgx, tgy, "left")
    time.sleep(wait)   # 等待转写文字出现
    return True


def transcribe_in_view(v, edge_filter=True, progress=None):
    """把当前这一屏可见的语音气泡逐条转文字。返回本屏成功点击「转文字」的条数。
    edge_filter=True 时只转中部气泡、跳过贴着上下边缘的——边缘气泡右键菜单会弹到
    屏幕外/被截断而失败；小步长向下滚时每个气泡都会从底部进入、经过中部，在中部转最稳。
    起始帧应传 edge_filter=False(顶部气泡再向下滚就移出去了，必须当场转)。
    已显示转写正文的语音不右键；菜单再次确认，只允许精确的新增转写操作。"""
    s = v.scale
    if edge_filter:
        margin = (v.reg["bottom_px"] - v.reg["top_px"]) * 0.14
        top_ok = v.win["y"] + (v.reg["top_px"] + margin) / s
        bot_ok = v.win["y"] + (v.reg["bottom_px"] - margin) / s
    else:
        top_ok, bot_ok = -1e9, 1e9
    n = 0
    candidates = find_candidates(v)
    if progress and getattr(v, 'voice_skipped', 0):
        progress(f"  · 此屏跳过 {v.voice_skipped} 条已有文字的语音，保留原转写")
    for (gx, gy) in candidates:
        if gy < top_ok or gy > bot_ok:   # 贴边的先跳过，等它滚到中部再转
            continue
        check_stop()
        if transcribe_one(v, gx, gy):
            n += 1
            v.refresh_geometry()
    return n


def transcribe_down(v, progress=print, max_steps=500):
    """从当前位置向下滚到底，逐屏把语音转成文字(转写会永久留在微信里)。
    返回 (向下滚动的步数, 累计转写条数)。必须先转写完、再单独拼图——否则边滚边转时
    转写文字出现在气泡下方、常已滚出当前帧，拍不进拼接图。"""
    from wechat_ui import match_shift
    n_voice = transcribe_in_view(v, edge_filter=False, progress=progress)
    if n_voice and progress:
        progress(f"  · 已转写 {n_voice} 条语音")
    steps = 0
    still = 0
    _, prev = v.grab()
    for i in range(max_steps):
        check_stop()
        v.scroll(-2); steps += 1        # 小步长：每个语音气泡在更多帧完整出现，多次检测机会
        got = transcribe_in_view(v, progress=progress)
        if got:
            n_voice += got
            if progress:
                progress(f"  · 已转写 {n_voice} 条语音")
        _, cur = v.grab()
        d, s = match_shift(cur, prev)   # 向下滚，内容上移量
        # 低分=视图被微信挪走(搜索跳转后的历史分块重新锚定)，是移动不是停滞；
        # 只有高分且无位移才计停滞，且多等几轮，避免把分块加载的假底部当成真到底
        if s >= 0.5 and d < 8:
            still += 1
            time.sleep(0.6)
            _, cur2 = v.grab()
            d2, s2 = match_shift(cur2, prev)
            if not (s2 >= 0.5 and d2 < 8):
                still = 0; cur = cur2
            elif still >= 5:
                break
        else:
            still = 0
        prev = cur
    return steps, n_voice
