#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2D连通域解析：气泡=闭运算后的连通块，时间戳来自OCR行，媒体=剩余内容块。
后处理：折行合并成段、时间/系统消息重分类、拼接缝重复去重、群聊昵称识别。"""
import re, sys
import numpy as np
from PIL import Image
from scipy import ndimage
from ocr import ocr_image, ocr_image_sliced

TIME_RE = re.compile(r'\d{1,2}月\d{1,2}日|昨天|前天|星期[一二三四五六日天]|周[一二三四五六日天]'
                     r'|上午|下午|凌晨|中午|晚上|\d{4}年|^\s*\d{1,2}:\d{2}\s*$')

# 整条内容(去空格后)就是一个完整时间戳：日期/时段可选，必须以 HH:MM 结尾
TIME_FULL_RE = re.compile(
    r'^(?:(?:\d{4}年)?\d{1,2}月\d{1,2}日|今天|昨天|前天'
    r'|星期[一二三四五六日天]|周[一二三四五六日天])?'
    r'(?:上午|下午|凌晨|中午|晚上)?\d{1,2}:\d{2}$')

# 语音时长：秒数+引号(1'')；视频/通话是冒号(1:05)，不匹配
VOICE_DUR_RE = re.compile(r'(\d{1,3})\s*(?:"|\'\'|″|”|’’|′|\')')

# 界面浮层噪声，不属于聊天内容
NOISE_RE = re.compile(r'^(以下[为是]新消息|查看更多消息|回到最新位置)$')

# 语音气泡内的操作按钮文字，不是消息内容
VOICE_UI_RE = re.compile(r'[\s•·]*(转文字|轉文字|暂停|播放)\s*$')

_ASCII_END = re.compile(r'[A-Za-z0-9]$')
_ASCII_BEGIN = re.compile(r'^[A-Za-z0-9]')


def _norm(s):
    return re.sub(r'\s+', '', s or '')


# ---- 文件卡片 ----
# 常见办公/压缩/媒体扩展名；后面不能紧跟字母数字，避免 ".doc" 命中 ".docker"
FILE_EXT_RE = re.compile(
    r'\.(docx?|xlsx?|pptx?|pdf|zip|rar|7z|txt|csv|json|xmind|apk|dmg|pkg|ipa'
    r'|numbers|pages|key|mp3|mp4|mov|avi|wav|m4a|psd|ai|svg|eps|dwg|sql'
    r'|py|js|ts|html?|css|md|log|epub|caj|wps|et|dps|rtf|xps)(?![A-Za-z0-9])', re.I)
# 卡片底部来源标签
FILE_SRC_RE = re.compile(r'微信(电脑|手机|Mac|Windows|iPhone|iPad|Android)版'
                         r'|已下载|未下载|下载中|文件已过期|已失效|接收中')
# 大小行(OCR 常把 0 认成 O、1 认成 l)，如 "28.OK" 实为 "28.0K"
FILE_SIZE_RE = re.compile(r'^[\d.,OoIl]{1,9}\s*(?:[KMGB]B?)$', re.I)
_SIZE_FIX = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1"})


def _join_wrapped(parts):
    """把被气泡宽度折断的文件名接回去；只在英数交界处补空格。"""
    out = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if out and _ASCII_END.search(out) and _ASCII_BEGIN.search(p):
            out += " "
        out += p
    return out


def take_file_card(rows):
    """文件卡片 = 文件名(可能折行) + 大小 + 「微信电脑版」等来源标签。

    必须同时出现【扩展名】和【大小或来源标签】才认定，这样正文里随口写的
    "我发你的 xx.docx" 不会被误判成文件卡片。返回 {"name","size"} 或 None。"""
    texts = [r["text"] for r in rows]
    hit = next(((i, m) for i, t in enumerate(texts)
                for m in [FILE_EXT_RE.search(t)] if m), None)
    if hit is None:
        return None
    i = hit[0]
    tail = texts[i + 1:]
    size = ""
    for t in tail:
        s = re.sub(r'\s+', '', t)
        if FILE_SIZE_RE.match(s):
            size = s.translate(_SIZE_FIX).upper()
            break
    if not size and not any(FILE_SRC_RE.search(t) for t in tail):
        return None
    name = _join_wrapped(texts[:i + 1])
    # 取最后一个扩展名并截断，甩掉图标字母("...docx W")和粘在同行的大小
    last = None
    for m in FILE_EXT_RE.finditer(name):
        last = m
    if last:
        name = name[:last.end()]
    return {"name": name.strip(), "size": size}


def take_voice_duration(rows):
    """气泡首/次行若是纯时长标记(可带"转文字"等按钮文字)，剥掉并返回时长；否则 None。"""
    for i in range(min(2, len(rows))):
        s = VOICE_UI_RE.sub('', rows[i]["text"].strip())
        if len(s) > 8:            # 转写正文比这长得多
            continue
        mm = VOICE_DUR_RE.search(s)
        if mm:
            rows.pop(i)
            return mm.group(1) + '"'
    return None


def build_masks(a):
    """内容掩膜 / 绿气泡(我) / 灰气泡(对方)。

    全程停在 uint8 上算。原来对每个通道做 .astype(int)：长会话的长图有 5600 万
    像素，一个通道转成 int64 就是 450MB，三个通道加上 max/min 中间量要搬 1.5GB
    内存，实测这一个函数占掉解析耗时的一半。改法与旧版逐位一致(已比对)：
    · |x-47|<11 直接展开成 36<x<58，不用求绝对值，也不用转类型；
    · mx-mn 恒非负，uint8 相减不会下溢；
    · 只有带符号的通道差(G-R、G-B)才开一个 int16 临时量。"""
    R, G, Bl = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    mx = a.max(axis=2)
    mn = a.min(axis=2)
    nonbg = (mx > 45) | ((mx - mn) > 12)
    Gi = G.astype(np.int16)
    green = ((Gi - R) > 40) & ((Gi - Bl) > 25)
    graybub = (((R > 36) & (R < 58)) & ((G > 36) & (G < 58))
               & ((Bl > 37) & (Bl < 59)))
    return nonbg, green, graybub


_H3 = np.ones((1, 3), bool)
_V3 = np.ones((3, 1), bool)
_S3 = np.ones((3, 3), bool)


def _closing(mask, structure, iters):
    """闭运算(先膨胀后腐蚀)，用小结构多次迭代代替一次大结构。

    形态学膨胀满足结合律：用 ones((1,3)) 迭代 n 次 == 用 ones((1,2n+1)) 一次，
    而 scipy 对「小结构 + iterations」有快得多的实现。实测长图上 2.8 倍提速，
    结果与原来逐位一致(已比对)。"""
    m = ndimage.binary_dilation(mask, structure=structure, iterations=iters)
    return ndimage.binary_erosion(m, structure=structure, iterations=iters)


def close_h(mask, hx=41, vy=9):
    """先水平闭(填字间空洞)再小幅竖直闭。"""
    m = _closing(mask, _H3, (hx - 1) // 2)
    return _closing(m, _V3, (vy - 1) // 2)


def bubbles_from(mask, sender, min_area_px, min_w, min_h):
    """连通块 → 气泡外接框。

    逐个标签做 np.where(lab == i) 会为每个连通块重扫整张图：长会话的长图有
    两千万像素、上百个连通块，实测这一个函数就占掉整次解析 60 秒里的 42 秒。
    find_objects 一趟给出全部外接框，bincount 一趟给出全部面积。"""
    lab, n = ndimage.label(mask)
    if n == 0:
        return []
    areas = np.bincount(lab.ravel(), minlength=n + 1)
    res = []
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        if sl is None or areas[i] < min_area_px:
            continue
        y0, y1 = sl[0].start, sl[0].stop - 1
        x0, x1 = sl[1].start, sl[1].stop - 1
        if (x1 - x0) < min_w or (y1 - y0) < min_h:
            continue
        res.append({"sender": sender, "x0": int(x0), "x1": int(x1),
                    "y0": int(y0), "y1": int(y1), "kind": "bubble"})
    return res


def group_rows(ls):
    """OCR 行聚合成视觉行，保留每行坐标范围；顺带去掉物理重复的行/段。"""
    rows = []
    for l in ls:
        if rows and abs(l["y"] - rows[-1]["y"]) < 18:
            r = rows[-1]
            # 同一视觉行里 x 范围重叠的相同文字＝重复识别，跳过
            if r["parts"] and l["text"] == r["parts"][-1] and l["x"] < r["x1"] - l["w"] * 0.3:
                continue
            r["parts"].append(l["text"])
            r["x0"] = min(r["x0"], l["x"]); r["x1"] = max(r["x1"], l["x"] + l["w"])
            r["h"] = max(r["h"], l["h"])
        else:
            rows.append({"y": l["y"], "x0": l["x"], "x1": l["x"] + l["w"],
                         "h": l["h"], "parts": [l["text"]]})
    out = []
    for r in rows:
        t = " ".join(r["parts"]).strip()
        if t:
            r["text"] = t
            out.append(r)
    # 相邻两行文字相同且 x 范围几乎一致、间距过小(拼接缝重复)，只留一行
    dedup = []
    for r in out:
        p = dedup[-1] if dedup else None
        if (p and r["text"] == p["text"] and abs(r["x0"] - p["x0"]) < 20
                and abs(r["x1"] - p["x1"]) < 20 and r["y"] - p["y"] < 26):
            continue
        dedup.append(r)
    return dedup


def merge_wrapped(rows, bubble_x1, wrap_pad=36):
    """把因气泡宽度自动折行的多行合并成自然段：
    一行右端顶到气泡右内边(说明是被折断的)且字号与下一行接近，则连成同一段。"""
    paras = []
    prev = None
    for r in rows:
        if prev is not None and prev["x1"] >= bubble_x1 - wrap_pad \
                and min(prev["h"], r["h"]) / max(prev["h"], r["h"], 1) > 0.72:
            sep = " " if (_ASCII_END.search(paras[-1]) and _ASCII_BEGIN.search(r["text"])) else ""
            paras[-1] += sep + r["text"]
        else:
            paras.append(r["text"])
        prev = r
    return paras


def find_sender_names(msgs, ocr_lines, skip_ids, boxes):
    """群聊：对方气泡/图片正上方左对齐的小字＝发送者昵称。1对1会话没有该标签，不受影响。"""
    def inside_any(cx, cy):
        for b in boxes:
            if b["x0"] - 4 <= cx <= b["x1"] + 4 and b["y0"] - 4 <= cy <= b["y1"] + 4:
                return True
        return False
    for m in msgs:
        if m.get("sender") != "them":
            continue
        for idx, l in enumerate(ocr_lines):
            if idx in skip_ids:
                continue
            bottom = l["y"] + l["h"]
            if not (m["y0"] - 30 <= bottom <= m["y0"] - 2):
                continue
            if abs(l["x"] - m["x0"]) > 44 or l["h"] > 30:
                continue
            t = l["text"].strip()
            if not t or len(t) > 24 or TIME_RE.search(t) or VOICE_DUR_RE.search(t):
                continue
            if inside_any(l["x"] + l["w"] / 2, l["y"] + l["h"] / 2):
                continue
            m["name"] = t
            skip_ids.add(idx)
            break


def parse_image(path, scale=2.0):
    im = Image.open(path).convert("RGB")
    im = im.crop((0, 0, im.width - 20, im.height))
    a = np.asarray(im)
    H, W = a.shape[:2]
    nonbg, green, graybub = build_masks(a)

    green_c = close_h(green)
    gray_c = close_h(graybub)
    bubbles = (bubbles_from(green_c, "me", 1500, 40, 24)
               + bubbles_from(gray_c, "them", 1500, 40, 24))

    # OCR(长图分片以保准确度)
    ocr_lines, _, _ = ocr_image_sliced(path)

    def text_in(x0, y0, x1, y1):
        got = []
        for l in ocr_lines:
            cx, cy = l["x"] + l["w"] / 2, l["y"] + l["h"] / 2
            if x0 - 8 <= cx <= x1 + 8 and y0 - 6 <= cy <= y1 + 6:
                got.append(l)
        got.sort(key=lambda l: (round(l["y"] / 12), l["x"]))
        return got

    # 时间戳：居中 + 命中正则 + 窄
    times = []
    used_line_ids = set()
    for idx, l in enumerate(ocr_lines):
        cx = l["x"] + l["w"] / 2
        if abs(cx - W / 2) < W * 0.14 and l["w"] < W * 0.5 and TIME_RE.search(l["text"]):
            times.append({"type": "time", "text": l["text"].strip(),
                          "y0": int(l["y"]), "y1": int(l["y"] + l["h"])})
            used_line_ids.add(idx)

    # 文本气泡取文字：视觉行聚合 → 剥语音时长 → 折行合并成段
    text_msgs = []
    for b in bubbles:
        rows = group_rows(text_in(b["x0"], b["y0"], b["x1"], b["y1"]))
        dur = take_voice_duration(rows)
        card = take_file_card(rows) if dur is None else None
        if card:
            text_msgs.append({"type": "file", "sender": b["sender"],
                              "fname": card["name"], "fsize": card["size"],
                              "text": card["name"],
                              "y0": b["y0"], "y1": b["y1"],
                              "x0": b["x0"], "x1": b["x1"]})
            continue
        text = "\n".join(merge_wrapped(rows, b["x1"])).strip()
        text_msgs.append({"type": "text", "sender": b["sender"], "text": text,
                          "voice": dur is not None, "dur": dur or "",
                          "y0": b["y0"], "y1": b["y1"], "x0": b["x0"], "x1": b["x1"]})

    # 媒体：内容掩膜里，扣掉气泡与时间戳后剩下的连通块
    occupied = np.zeros((H, W), bool)
    for b in bubbles:
        occupied[b["y0"]:b["y1"]+1, b["x0"]:b["x1"]+1] = True
    for t in times:
        occupied[max(0, t["y0"]-4):t["y1"]+4, :] = True
    # 头像列也算"内容"但不是媒体主体；仍保留在content里以定位，但媒体块要够大
    media_mask = nonbg & (~occupied)
    media_mask = _closing(media_mask, _S3, 2)   # ones((3,3))迭代2次 == ones((5,5))
    lab, n = ndimage.label(media_mask)
    media_msgs = []
    m_areas = np.bincount(lab.ravel(), minlength=n + 1) if n else np.zeros(1, int)
    for i, sl in enumerate(ndimage.find_objects(lab) if n else [], start=1):
        if sl is None:
            continue
        y0, y1 = sl[0].start, sl[0].stop - 1
        x0, x1 = sl[1].start, sl[1].stop - 1
        w, h = x1 - x0, y1 - y0
        area = m_areas[i]
        if area < 6000 or w < 70 or h < 70:   # 过滤小碎块(头像残留/图标)
            continue
        # 头像本身也会成块(~74x74)，靠尺寸区分：媒体通常更大或在中部
        sender = "me" if (x0 + x1) / 2 > W * 0.52 else "them"
        media_msgs.append({"type": "media", "sender": sender,
                           "y0": int(y0), "y1": int(y1), "x0": int(x0), "x1": int(x1)})

    # 清理：去浮层噪声；居中窄块重分类为时间/系统消息(深色模式下居中灰字
    # 的抗锯齿像素会被误检成"对方"气泡，如时间戳、"已建群"、撤回提示)
    cleaned = []
    for m in text_msgs:
        if m["type"] == "file":      # 文件卡片不参与"居中=系统消息"重分类
            cleaned.append(m)
            continue
        t = re.sub(r'[＆&]?\s*\d+\s*条新消息', '', m.get("text", "")).strip()
        if not t and not m.get("voice"):
            continue
        m["text"] = t
        if not m.get("voice"):
            cx = (m["x0"] + m["x1"]) / 2
            centered = abs(cx - W / 2) < W * 0.14 and (m["x1"] - m["x0"]) < W * 0.5
            if centered:
                if NOISE_RE.match(_norm(t)):
                    continue
                if TIME_FULL_RE.match(_norm(t)):
                    cleaned.append({"type": "time", "text": t, "y0": m["y0"], "y1": m["y1"]})
                    continue
                if "\n" not in t and len(_norm(t)) <= 30:
                    cleaned.append({"type": "system", "text": t, "y0": m["y0"], "y1": m["y1"]})
                    continue
        cleaned.append(m)
    text_msgs = cleaned

    # 剔除"假气泡"：群聊里每条消息上方的昵称标签、图片里的文字，它们的抗锯齿灰度
    # 正好落在灰气泡的色区里，会被连通域当成气泡。真气泡有内边距，单行也有固定
    # 高度(实测 73px@2x)，这些假货则很扁(实测 h=24~40)。阈值取本次所有文本气泡
    # 高度的中位数比例，与缩放、主题无关。
    # 只按高度判，不能顺带按宽度判：连通域经常把左边的头像和气泡粘成一块
    # (实测 x=20~1853、w=1833 的真消息)，按宽度删会把真消息整条删掉。
    th = sorted(m["y1"] - m["y0"] for m in text_msgs if m["type"] == "text")
    if th:
        min_h = th[len(th) // 2] * 0.6
        text_msgs = [m for m in text_msgs
                     if m["type"] != "text" or m.get("voice")
                     or (m["y1"] - m["y0"]) >= min_h]

    # 媒体块若与某文本气泡高度重叠(说明是同一气泡的误检)，丢弃媒体
    def overlap(m, b):
        lo, hi = max(m["y0"], b["y0"]), min(m["y1"], b["y1"])
        return max(0, hi - lo)
    media_kept = []
    text_boxes = [m for m in text_msgs if m["type"] in ("text", "file")]
    for m in media_msgs:
        mh = m["y1"] - m["y0"]
        if any(overlap(m, b) > 0.6 * mh for b in text_boxes):
            continue
        media_kept.append(m)
    media_msgs = media_kept

    # 群聊昵称：气泡/媒体上方的小字标签
    find_sender_names(text_boxes + media_msgs, ocr_lines, set(used_line_ids),
                      text_boxes + media_msgs)

    # 居中、又不属于任何气泡/媒体/时间戳的 OCR 行 = 系统提示("XX 邀请你加入了群聊"、
    # "XX 撤回了一条消息"、"已建群")。这些字是直接画在背景上的，没有气泡底色，
    # 连通域一个也抓不到，必须从 OCR 行里单独捡回来，否则建群、撤回整条丢失。
    boxes = [(m["y0"], m["y1"], m["x0"], m["x1"])
             for m in text_msgs + media_msgs if "x0" in m]
    sys_msgs = []
    for idx, l in enumerate(ocr_lines):
        if idx in used_line_ids:
            continue
        cx, cy = l["x"] + l["w"] / 2, l["y"] + l["h"] / 2
        if abs(cx - W / 2) > W * 0.12 or l["w"] > W * 0.72:
            continue
        if any(y0 - 6 <= cy <= y1 + 6 and x0 - 8 <= cx <= x1 + 8
               for y0, y1, x0, x1 in boxes):
            continue
        t = l["text"].strip()
        if not t or NOISE_RE.match(_norm(t)) or TIME_FULL_RE.match(_norm(t)):
            continue
        sys_msgs.append({"type": "system", "text": t,
                         "y0": int(l["y"]), "y1": int(l["y"] + l["h"])})

    msgs = text_msgs + times + media_msgs + sys_msgs
    msgs.sort(key=lambda m: m["y0"])

    # 相邻重复去重：同一时间分隔符连续出现(OCR 双检/拼接缝)只留一个；
    # 拼接续接产生的相邻同文本气泡(仅长文本，避免误删真的重复发言)只留一个
    out = []
    for m in msgs:
        p = out[-1] if out else None
        if (p and m["type"] in ("time", "system") and p["type"] == m["type"]
                and _norm(p["text"]) == _norm(m["text"])):
            continue
        if (p and m["type"] == "text" and p["type"] == "text"
                and p.get("sender") == m.get("sender") and not m.get("voice")
                and len(_norm(m.get("text", ""))) >= 8
                and _norm(p.get("text", "")) == _norm(m.get("text", ""))
                and m["y0"] - p["y1"] < 220):
            continue
        out.append(m)

    # 群聊昵称向后沿用：标签只识别到一部分时，同一人的连续消息也能带上名字
    last_name = None
    for m in out:
        if m["type"] in ("text", "media") and m.get("sender") == "them":
            if m.get("name"):
                last_name = m["name"]
            elif last_name:
                m["name"] = last_name
    return im, out


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "stitched.png"
    im, msgs = parse_image(path)
    print(f"共 {len(msgs)} 条:")
    for m in msgs:
        t = (m.get("text") or "").replace("\n", "⏎")[:50]
        if m["type"] == "file":
            t = f"📎 {m['fname']} ({m['fsize']})"
        xr = f" x[{m.get('x0','?')}-{m.get('x1','?')}]" if m["type"] not in ("time", "system") else ""
        v = f" 🎤{m['dur']}" if m.get("voice") else ""
        nm = f" @{m['name']}" if m.get("name") else ""
        print(f"  [{m['y0']:5d}-{m['y1']:5d}] {m['type']:6s}/{m.get('sender','-'):5s}{nm}{xr}{v} {t}")
