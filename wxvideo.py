#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""聊天里的视频：认出气泡对应磁盘上的哪个文件、没下载的自动点开下载、导出时复制出来。

微信 4.x 把视频【明文】存在容器里，按月分目录：
    ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/
      xwechat_files/<wxid>/msg/video/YYYY-MM/
        <hash>_thumb.jpg   气泡里显示的那张封面缩略图(消息一到就有，永远在)
        <hash>.jpg         大图封面   ┐ 只有【下载过】(在电脑上点开看过)的才有
        <hash>.mp4         视频本体   ┘
        <hash>_raw.mp4     原画大文件(点过「查看原视频」才有，通常比 .mp4 大十几倍)

文件名是哈希，跟聊天内容对不上号，所以按【封面图像】认人：把长图里的视频气泡
和磁盘上的 _thumb.jpg 逐一做归一化互相关(NCC)。三个实测得出的关键点：

· 必须【竖直滑动】对齐再比。气泡里显示的封面常常是缩略图竖直方向的一段
  (微信给气泡限高)，直接按整图比会错位：同一条视频，滑动对齐能拿 0.991，
  不对齐只有 0.763。
· 必须【挖掉正中间的播放按钮】。那个白色圆钮是微信画上去的，缩略图里没有，
  占掉中心约 5% 面积却全是高对比边缘，特别吃相关分。挖掉后 8 条视频的真匹配
  是 0.989~0.996，不挖有一条掉到 0.763。
· 分数天然分得很开(实测 466 个视频的库)：真匹配 0.989~0.996，同一条的第二名
  0.71~0.95；而真·图片气泡(不是视频)最高只有 0.770。所以 0.93 这条线两边都很空，
  既不会把图片当成视频，也不会漏掉视频。
"""
import os, shutil, struct, time
import numpy as np
from PIL import Image

CONTAINER = os.path.expanduser(
    "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files")

TW = 48                # 比对时统一缩到这个宽度(再细也不会更准，只会更慢)
MATCH_MIN = 0.93       # 认定「就是这条视频」的最低分
NEAR_TIE = 0.02        # 与第二名差距小于此值时，按聊天的时间顺序定夺
MIN_BLOCK_PX = 90      # 视频气泡最小边长(2x 像素)；再小的是头像/表情


def video_roots():
    """所有已登录账号的 msg/video 目录。"""
    roots = []
    if not os.path.isdir(CONTAINER):
        return roots
    for acc in sorted(os.listdir(CONTAINER)):
        d = os.path.join(CONTAINER, acc, "msg", "video")
        if os.path.isdir(d):
            roots.append(d)
    return roots


# ---------------------------------------------------------------- 时长
def mp4_duration(path):
    """从 mp4 头部的 mvhd 读时长(秒)。读不到返回 None(不猜)。"""
    try:
        size_total = os.path.getsize(path)
        with open(path, "rb") as f:
            def walk(off, limit, depth):
                while off + 8 <= limit and depth <= 3:
                    f.seek(off)
                    hdr = f.read(8)
                    if len(hdr) < 8:
                        return None
                    size = struct.unpack(">I", hdr[:4])[0]
                    typ = hdr[4:8]
                    head = 8
                    if size == 1:
                        ext = f.read(8)
                        if len(ext) < 8:
                            return None
                        size = struct.unpack(">Q", ext)[0]
                        head = 16
                    elif size == 0:
                        size = limit - off
                    if size < head or off + size > limit:
                        return None
                    if typ == b"mvhd":
                        ver = f.read(4)[0]
                        if ver == 1:
                            b = f.read(28)
                            ts, dur = struct.unpack(">IQ", b[16:28])
                        else:
                            b = f.read(16)
                            ts, dur = struct.unpack(">II", b[8:16])
                        return dur / ts if ts else None
                    if typ in (b"moov", b"trak", b"mdia"):
                        got = walk(off + head, off + size, depth + 1)
                        if got is not None:
                            return got
                    off += size
                return None
            return walk(0, size_total, 0)
    except Exception:
        return None


def fmt_dur(sec):
    if not sec or sec < 0:
        return ""
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def human_size(n):
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0


# ---------------------------------------------------------------- 匹配
def _prep(img):
    """灰度 + 缩到统一宽度。宽度统一之后，竖直滑动就能覆盖「气泡是缩略图的一段」。"""
    g = img.convert("L")
    h = max(1, int(round(g.height * TW / g.width)))
    return np.asarray(g.resize((TW, h), Image.LANCZOS), dtype=np.float32)


def _play_button_mask(h):
    """挖掉正中间那枚播放按钮(微信画在封面上的白色圆钮，缩略图里没有)。"""
    m = np.ones((h, TW), bool)
    yy, xx = np.mgrid[0:h, 0:TW]
    r = TW * 0.30
    m[((yy - h / 2.0) ** 2 + (xx - TW / 2.0) ** 2) < r * r] = False
    return m


def _ncc(a, b, mask):
    if mask is not None:
        a = a[mask]; b = b[mask]
    a = a - a.mean(); b = b - b.mean()
    na = float(np.sqrt((a * a).sum())); nb = float(np.sqrt((b * b).sum()))
    if na < 1e-6 or nb < 1e-6:
        return -1.0
    return float((a * b).sum() / (na * nb))


def _slide(B, T, mask):
    """B(气泡) 与 T(缩略图) 竖直滑动取最高分。短的那张在长的那张上滑。"""
    hb, ht = B.shape[0], T.shape[0]
    best = -1.0
    if ht >= hb:
        for d in range(0, ht - hb + 1):
            s = _ncc(B, T[d:d + hb], mask)
            if s > best:
                best = s
    else:
        for d in range(0, hb - ht + 1):
            s = _ncc(B[d:d + ht], T, None if mask is None else mask[d:d + ht])
            if s > best:
                best = s
    return best


class VideoIndex:
    """磁盘上所有视频的索引：封面缩略图(用来认人) + 本体是否已下载。"""

    def __init__(self, roots=None):
        self.entries = []
        for root in (roots if roots is not None else video_roots()):
            for month in sorted(os.listdir(root)):
                d = os.path.join(root, month)
                if not os.path.isdir(d):
                    continue
                try:
                    names = os.listdir(d)
                except OSError:
                    continue
                for fn in names:
                    if not fn.endswith("_thumb.jpg"):
                        continue
                    stem = fn[:-len("_thumb.jpg")]
                    thumb = os.path.join(d, fn)
                    try:
                        arr = _prep(Image.open(thumb))
                    except Exception:
                        continue          # 缩略图坏了就跳过，不影响别的
                    self.entries.append({
                        "stem": stem, "dir": d, "month": month,
                        "thumb": thumb, "arr": arr,
                        "mtime": os.path.getmtime(thumb),
                    })
        self.entries.sort(key=lambda e: e["mtime"])
        self.by_stem = {e["stem"]: e for e in self.entries}
        self.refresh()

    def __len__(self):
        return len(self.entries)

    def refresh(self):
        """重新看一遍哪些已经下载到本地了(下载过程中会变)。"""
        for e in self.entries:
            d, stem = e["dir"], e["stem"]
            mp4 = os.path.join(d, stem + ".mp4")
            raw = os.path.join(d, stem + "_raw.mp4")
            cover = os.path.join(d, stem + ".jpg")
            e["mp4"] = mp4 if os.path.exists(mp4) else None
            e["raw"] = raw if os.path.exists(raw) else None
            e["cover"] = cover if os.path.exists(cover) else None

    @staticmethod
    def best_file(e):
        """导出用哪个文件：优先原画(点过「查看原视频」才有)，否则标清。"""
        return e.get("raw") or e.get("mp4")

    def match(self, crop):
        """气泡截图 → (entry, 分数, 与第二名的差距)。认不出返回 (None, 分数, 0)。"""
        if crop.width < 8 or crop.height < 8 or not self.entries:
            return None, 0.0, 0.0
        B = _prep(crop)
        mask = _play_button_mask(B.shape[0])
        scores = [(_slide(B, e["arr"], mask), e) for e in self.entries]
        scores.sort(key=lambda t: -t[0])
        top, e = scores[0]
        second = scores[1][0] if len(scores) > 1 else -1.0
        if top < MATCH_MIN:
            return None, top, 0.0
        return e, top, top - second

    def candidates(self, crop, limit=6):
        """认得出的前几名(分数都过线的)，给「按时间顺序定夺」用。"""
        if not self.entries:
            return []
        B = _prep(crop)
        mask = _play_button_mask(B.shape[0])
        scores = [(_slide(B, e["arr"], mask), e) for e in self.entries]
        scores.sort(key=lambda t: -t[0])
        return scores[:limit]


# ---------------------------------------------------------------- 一帧里找媒体块
def media_blocks(a, min_px=MIN_BLOCK_PX, max_blocks=14):
    """一帧(或长图的一段)里的图片/视频块。a: (H,W,3) uint8。返回 [(x0,y0,x1,y1)]。

    聊天区背景是一整片纯色，所以「离背景色远」的连通块就是内容；媒体块是实心
    矩形，按大小和填充率挑出来。文字气泡也会被挑中，但它跟任何视频封面都对不上，
    匹配那一步自然落选。
    竖直方向裂成几块的要拼回去：封面里若有一条颜色接近背景的暗带(实测有)，
    连通域会从那里断开，只拿上半截去比对就认不出来了。"""
    from scipy import ndimage
    bg = np.median(a[::7, ::7].reshape(-1, 3), axis=0)
    diff = np.abs(a.astype(np.int16) - bg.astype(np.int16)).max(axis=2)
    lab, n = ndimage.label(diff > 24)
    if not n:
        return []
    areas = np.bincount(lab.ravel(), minlength=n + 1)
    raw = []
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        if sl is None:
            continue
        y0, y1 = int(sl[0].start), int(sl[0].stop)
        x0, x1 = int(sl[1].start), int(sl[1].stop)
        w, h = x1 - x0, y1 - y0
        if w < min_px or h < min_px or areas[i] < 0.55 * w * h:
            continue
        raw.append([x0, y0, x1, y1])
    raw.sort(key=lambda b: b[1])
    out = []
    for b in raw:
        if out:
            p = out[-1]
            ov = min(p[2], b[2]) - max(p[0], b[0])
            if ov > 0.6 * min(p[2] - p[0], b[2] - b[0]) and b[1] - p[3] < 14:
                p[0] = min(p[0], b[0]); p[2] = max(p[2], b[2]); p[3] = b[3]
                continue
        out.append(b)
    return [tuple(b) for b in out[:max_blocks]]


def match_in(crop, entries):
    """只在给定的几条里认。返回 (entry, 分数)；认不出 (None, 分数)。"""
    if crop.width < 8 or crop.height < 8 or not entries:
        return None, 0.0
    B = _prep(crop)
    mask = _play_button_mask(B.shape[0])
    best, be = -1.0, None
    for e in entries:
        s = _slide(B, e["arr"], mask)
        if s > best:
            best, be = s, e
    return (be if best >= MATCH_MIN else None), best


# ---------------------------------------------------------------- 点开下载
def _mp4_snapshot(roots=None):
    """当前磁盘上所有视频本体的路径集合。"""
    got = set()
    for root in (roots if roots is not None else video_roots()):
        for month in os.listdir(root):
            d = os.path.join(root, month)
            if not os.path.isdir(d):
                continue
            try:
                for fn in os.listdir(d):
                    if fn.endswith(".mp4"):
                        got.add(os.path.join(d, fn))
            except OSError:
                pass
    return got


def _sub_windows(pid):
    """微信除主窗之外还开着的窗口(点开视频后的「图片和视频」预览窗)。"""
    import Quartz
    from geometry import MAIN_TITLES
    ws = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID) or []
    out = []
    for w in ws:
        if w.get("kCGWindowOwnerPID") != pid or w.get("kCGWindowLayer", 0) != 0:
            continue
        name = (w.get("kCGWindowName") or "").strip()
        if name in MAIN_TITLES:
            continue
        b = w["kCGWindowBounds"]
        if b["Width"] < 200 or b["Height"] < 150:
            continue          # 输入法候选条之类的小浮窗不算
        out.append({"id": w["kCGWindowNumber"], "name": name})
    return out


def _click(gx, gy):
    import Quartz
    from wechat_ui import move_mouse, note_self_click
    note_self_click()      # 声明是工具自己点的，别被「连点鼠标=停止」误伤
    move_mouse(gx, gy)
    for ev in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(
            None, ev, Quartz.CGPointMake(gx, gy), Quartz.kCGMouseButtonLeft))
        time.sleep(0.08)


def _key(code, cmd=False):
    import Quartz
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
        if cmd:
            Quartz.CGEventSetFlags(ev, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.04)


def _close_preview(pid, tries=4):
    """关掉视频预览窗。先 Esc，不行再 ⌘W。返回是否关掉了。"""
    for i in range(tries):
        if not _sub_windows(pid):
            return True
        if i % 2 == 0:
            _key(53)              # Esc
        else:
            _key(13, cmd=True)    # ⌘W
        time.sleep(0.45)
    return not _sub_windows(pid)


def download_one(gx, gy, progress=None, timeout=180, settle=1.5, tries=3):
    """点开某条视频的气泡把它下下来，下完关掉预览窗。

    微信没有「只下载不播放」的入口：点开 = 边下边播，所以只能点一下、盯着
    磁盘上的 mp4 长大、长到不动了就关窗。返回新下载出来的 mp4 路径(没下成 None)。
    身份不靠「我以为点的是哪条」来定，而是看【新冒出来的是哪个文件】——
    万一气泡认错了人，这里也不会把别人的视频记到这条头上。

    点一下不一定算数：微信刚被切到前台那会儿，第一次点击常常被窗口激活吃掉
    (实测同一个气泡，第一次点下去毫无反应，紧接着第二次就弹出了预览窗)。
    所以给 5 秒，既没弹预览窗又没出新文件就再点一次，最多 tries 次。"""
    from wechat_ui import wechat_pid, check_stop, ensure_front_or_raise
    say = progress or (lambda *_: None)
    pid = wechat_pid()
    before = _mp4_snapshot()
    ensure_front_or_raise()
    _click(gx, gy)
    t0 = time.time()
    clicks, last_click = 1, time.time()
    newfile = None
    last_size, stable_since = -1, None
    try:
        while time.time() - t0 < timeout:
            check_stop()
            if newfile is None:
                fresh = _mp4_snapshot() - before
                if fresh:
                    newfile = max(fresh, key=os.path.getmtime)
                elif not _sub_windows(pid) and time.time() - last_click > 5:
                    if clicks >= tries:
                        break      # 点了几次都没反应，这一下多半点空了
                    clicks += 1
                    last_click = time.time()
                    _click(gx, gy)
            if newfile is not None:
                try:
                    size = os.path.getsize(newfile)
                except OSError:
                    size = -1
                if size > 0 and size == last_size:
                    if stable_since is None:
                        stable_since = time.time()
                    elif time.time() - stable_since >= settle:
                        break      # 大小不再长 = 下完了
                else:
                    stable_since = None
                    last_size = size
            time.sleep(0.4)
    finally:
        # 不管是下完了、超时了，还是用户中途喊停，预览窗都必须关掉：
        # 它顶在主窗前面，不关的话后面的滚轮全打在它身上，抓取就停在原地了。
        if not _close_preview(pid):
            say("  ! 视频预览窗没关掉，请手动按一下 Esc")
        time.sleep(0.4)
    return newfile if newfile and os.path.exists(newfile) else None


def download_missing(v, entries, progress=print, max_steps=400, limit=60,
                     max_total_mb=3000):
    """从当前位置【向上】翻，把 entries(还没下载的那些)挨个点开下载。

    特意放在拼长图【之后】做：点开视频会弹预览窗，收掉之后微信有可能把聊天
    视图挪了位置——那时候长图已经拼完了，挪了也不影响结果。
    每屏只跟「还没下到」的那几条比对，所以很快；找齐了就立刻收工。"""
    from wechat_ui import match_shift, check_stop, ensure_front_or_raise
    s = v.scale
    remain = list(entries)
    done, failed, total_mb = [], [], 0.0
    still = 0
    # 开工前先等画面停稳。微信是平滑滚动，刚滚过一下就抓帧的话，抓到的是动画
    # 中间态——按它算出来的气泡位置已经过时，点下去会落到气泡外面的空白上，
    # 看起来就是「点了没反应」(实测在 calibrate_bottom 之后紧接着点，连点三次都没反应)。
    _, prev = v.grab()
    for _ in range(8):
        time.sleep(0.15)
        _, g2 = v.grab()
        if v.frames_settled(prev, g2):
            prev = g2
            break
        prev = g2
    for step in range(max_steps):
        if not remain or len(done) >= limit:
            break
        check_stop()
        rgb, gray = v.grab()
        a = np.asarray(rgb)
        for (x0, y0, x1, y1) in media_blocks(a):
            if not remain:
                break
            # 贴着上下边缘的先放过：等它翻到中间再认，认得准、点得准
            if y0 < 4 or y1 > a.shape[0] - 4:
                continue
            e, sc = match_in(rgb.crop((x0, y0, x1, y1)), remain)
            if e is None:
                continue
            gx = v.win["x"] + (v.reg["pane_x_px"] + (x0 + x1) / 2) / s
            gy = v.win["y"] + (v.reg["top_px"] + (y0 + y1) / 2) / s
            progress(f"  · 发现没下载的视频，正在点开下载({len(done)+1}/{len(entries)})...")
            got = download_one(gx, gy, progress=progress)
            if got:
                mb = os.path.getsize(got) / 1024.0 / 1024.0
                total_mb += mb
                stem = os.path.basename(got)[:-4]
                for r in list(remain):
                    if r["stem"] in (stem, stem.replace("_raw", "")):
                        remain.remove(r); done.append(r); break
                else:
                    remain.remove(e); done.append(e)
                progress(f"  · 已下载 {os.path.basename(got)[:12]}… {human_size(os.path.getsize(got))}")
            else:
                remain.remove(e); failed.append(e)
                progress("  · 这条没下下来(可能已过期)，跳过")
            v.refresh_geometry()
            if total_mb >= max_total_mb:
                progress(f"  ! 本次已下载 {total_mb:.0f}MB，达到上限，剩下的不再下了")
                remain = []
                break
            _, prev = v.grab()
            break            # 屏幕可能被预览窗动过，这一屏重新来过
        else:
            ensure_front_or_raise()
            _, cur = v.scroll_settled(3, steps=3, before=prev)   # 正数=向上
            d, score = match_shift(prev, cur)
            if score >= 0.5 and d < 8:
                still += 1
                time.sleep(0.8)
                if still >= 6:
                    break        # 翻到会话开头了
            else:
                still = 0
            prev = cur
    return done, failed, remain


# ---------------------------------------------------------------- 导出
DUR_RE = None      # 延迟编译，见下


def _dur_re():
    global DUR_RE
    if DUR_RE is None:
        import re
        DUR_RE = re.compile(r'^\s*\d{1,2}:\d{2}\s*$')
    return DUR_RE


def resolve_videos(im, msgs, index=None, progress=None):
    """在解析结果里把视频认出来：media → video，补上时长/磁盘路径/封面。

    顺手收拾两件解析层面的碎事(都是实测遇到的)：
    · 封面里若有一条接近背景色的暗带，连通域会把一个气泡切成上下两块，
      两块认出来是同一条视频，合并回一条；
    · 气泡右下角那枚时长标签(「0:36」)有自己的深色小底，会被当成一条独立的
      灰气泡消息，认完之后并进视频里、从消息流中删掉。
    返回 (认出的视频条数, 其中本机还没有的条数)。"""
    say = progress or (lambda *_: None)
    idx = index if index is not None else VideoIndex()
    media = [m for m in msgs if m["type"] == "media"]
    if not media or not len(idx):
        return 0, 0
    pending = []
    for m in media:
        cands = idx.candidates(im.crop((m["x0"], m["y0"], m["x1"], m["y1"])))
        if not cands or cands[0][0] < MATCH_MIN:
            continue
        near = [e for sc, e in cands if cands[0][0] - sc <= NEAR_TIE]
        pending.append((m, cands[0][0], near))
    # 打平手的(同一条视频转发过两次、两段几乎一样的录屏)按时间顺序定夺：
    # 聊天里越靠下的视频，收到的时间一定越晚，缩略图落盘的时间也就越晚。
    for m, sc, near in pending:
        m["_vchoices"] = near
        m["vconf"] = round(sc, 3)
    anchors = [(i, p[2][0]["mtime"]) for i, p in enumerate(pending) if len(p[2]) == 1]
    for i, (m, sc, near) in enumerate(pending):
        if len(near) == 1:
            chosen = near[0]
        else:
            lo = max([t for j, t in anchors if j < i], default=None)
            hi = min([t for j, t in anchors if j > i], default=None)
            fit = [e for e in near
                   if (lo is None or e["mtime"] >= lo) and (hi is None or e["mtime"] <= hi)]
            chosen = (fit or near)[0]
        m.pop("_vchoices", None)
        m["type"] = "video"
        m["vstem"] = chosen["stem"]
        m["vdir"] = chosen["dir"]
        m["vpath"] = VideoIndex.best_file(chosen)
        m["vcover"] = chosen.get("cover") or chosen["thumb"]
        m["vhd"] = bool(chosen.get("raw"))
        if m["vpath"]:
            m["vsize"] = human_size(os.path.getsize(m["vpath"]))
            m["vdur"] = fmt_dur(mp4_duration(m["vpath"]))
        else:
            m["vsize"] = ""
            m["vdur"] = ""
            m["vnote"] = "本机没有这个视频(没在电脑上点开过，或已超过微信的保存期)"
    # 一个气泡被切成两块 → 合并。封面里只要有一条颜色接近聊天背景的暗带，
    # 连通域就会从那儿断开：实测一条 1:04 的视频被切成 277x353 和 277x91 两块，
    # 下面那条小的谁也认不出来，不收进来就会当成一张莫名其妙的图片导出去。
    vids = [m for m in msgs if m["type"] == "video"]
    drop = set()

    def _absorb(vd, other):
        ov = min(vd["x1"], other["x1"]) - max(vd["x0"], other["x0"])
        if ov < 0.7 * min(vd["x1"] - vd["x0"], other["x1"] - other["x0"]):
            return False
        gap = other["y0"] - vd["y1"] if other["y0"] >= vd["y1"] else vd["y0"] - other["y1"]
        if not (0 <= gap <= 60):
            return False
        vd["y0"] = min(vd["y0"], other["y0"]); vd["y1"] = max(vd["y1"], other["y1"])
        vd["x0"] = min(vd["x0"], other["x0"]); vd["x1"] = max(vd["x1"], other["x1"])
        return True

    for a, b in zip(vids, vids[1:]):
        if a["vstem"] == b["vstem"] and _absorb(a, b):
            drop.add(id(b))
    for m in msgs:
        if m["type"] != "media" or id(m) in drop:
            continue
        for vd in vids:
            if id(vd) in drop or (m["y1"] - m["y0"]) > 0.6 * (vd["y1"] - vd["y0"]):
                continue
            if _absorb(vd, m):
                drop.add(id(m))
                break
    # 时长标签并进来
    for m in list(msgs):
        if m["type"] != "text" or not _dur_re().match(m.get("text") or ""):
            continue
        for vd in vids:
            if id(vd) in drop:
                continue
            if (vd["x0"] - 12 <= m.get("x0", -1) and m.get("x1", 1 << 30) <= vd["x1"] + 12
                    and vd["y0"] - 10 <= m["y0"] <= vd["y1"] + 90):
                if not vd.get("vdur"):
                    vd["vdur"] = m["text"].strip()
                drop.add(id(m))
                break
    if drop:
        msgs[:] = [m for m in msgs if id(m) not in drop]
    vids = [m for m in msgs if m["type"] == "video"]
    miss = sum(1 for m in vids if not m.get("vpath"))
    if vids:
        say(f"· 认出 {len(vids)} 条视频" + (f"，其中 {miss} 条本机还没有" if miss else "，都在本机"))
    return len(vids), miss


def reattach_videos(msgs):
    """自动下载跑完之后，把新到的文件重新挂到消息上。返回补齐的条数。"""
    n = 0
    for m in msgs:
        if m.get("type") != "video" or m.get("vpath"):
            continue
        d, stem = m.get("vdir"), m.get("vstem")
        if not d or not stem:
            continue
        got = next((p for p in (os.path.join(d, stem + "_raw.mp4"),
                                os.path.join(d, stem + ".mp4")) if os.path.exists(p)), None)
        if not got:
            continue
        cover = os.path.join(d, stem + ".jpg")
        if os.path.exists(cover):
            m["vcover"] = cover
        m["vpath"] = got
        m["vsize"] = human_size(os.path.getsize(got))
        m["vdur"] = m.get("vdur") or fmt_dur(mp4_duration(got))
        m["vhd"] = got.endswith("_raw.mp4")
        m.pop("vnote", None)
        n += 1
    return n


def dur_name(d):
    """时长做成文件名里那一小段：0:36 → 36秒；1:04 → 1分04秒。"""
    if not d or ":" not in d:
        return ""
    mm, ss = d.split(":", 1)
    return f"{ss}秒" if mm in ("0", "00") else f"{mm}分{ss}秒"


def save_poster(m, media_dir, seq, im=None):
    """把视频封面放进导出文件夹，返回文件路径。

    优先用微信本地存的那张封面(清楚、而且没有微信画上去的播放按钮)；
    没有就退回长图上的气泡截图。"""
    os.makedirs(media_dir, exist_ok=True)
    src = m.get("vcover")
    if src and os.path.exists(src):
        dst = os.path.join(media_dir, f"video_{seq:03d}.jpg")
        try:
            # 必须用 PIL 转存一遍，不能直接复制：微信那张大图封面是 ffmpeg 抽帧
            # 出来的裸 JPEG(开头 FFD8 FFFE，一段 COM 注释)，Word 的图片识别只认
            # JFIF/EXIF 这两种开头，直接塞进 .docx 会抛 UnrecognizedImageError，
            # 结果就是文档里一张视频封面都没有(实测踩过)。
            Image.open(src).convert("RGB").save(dst, "JPEG", quality=88)
            return dst
        except Exception:
            pass
    dst = os.path.join(media_dir, f"video_{seq:03d}.png")
    if im is not None:
        im.crop((m["x0"], m["y0"], m["x1"], m["y1"])).save(dst)
        return dst
    return None


def collect_videos(msgs, dest_dir=None, max_copy_mb=500, progress=None):
    """把视频复制进本次导出的文件夹；dest_dir 为空则只在文档里写微信原始位置。
    返回 (复制/定位成功数, 视频总数)。"""
    say = progress or (lambda *_: None)
    vids = [m for m in msgs if m.get("type") == "video"]
    if not vids:
        return 0, 0
    hit = 0
    for i, m in enumerate(vids, start=1):
        src = m.get("vpath")
        if not src or not os.path.exists(src):
            continue
        hit += 1
        if not dest_dir:
            continue
        size = os.path.getsize(src)
        if size > max_copy_mb * 1024 * 1024:
            m["vnote"] = f"视频 {human_size(size)} 过大，未复制，见微信原始位置"
            continue
        os.makedirs(dest_dir, exist_ok=True)
        tag = dur_name(m.get("vdur"))
        name = f"视频_{i:03d}" + (f"_{tag}" if tag else "")
        dst = os.path.join(dest_dir, name + os.path.splitext(src)[1])
        if not os.path.exists(dst) or os.path.getsize(dst) != size:
            try:
                shutil.copy2(src, dst)
            except OSError as err:
                m["vnote"] = f"复制失败: {err}"
                continue
        m["vcopy"] = os.path.join(os.path.basename(dest_dir), os.path.basename(dst))
    if dest_dir:
        say(f"· 视频复制完成：{hit}/{len(vids)} 条")
    return hit, len(vids)


if __name__ == "__main__":
    import sys
    idx = VideoIndex()
    have = sum(1 for e in idx.entries if VideoIndex.best_file(e))
    print(f"索引到 {len(idx)} 条视频(已下载 {have}，未下载 {len(idx)-have})，来自:")
    for r in video_roots():
        print("  ", r)
    for path in sys.argv[1:]:
        from parse3 import parse_image
        im, msgs = parse_image(path)
        n, miss = resolve_videos(im, msgs, idx, progress=print)
        for m in msgs:
            if m.get("type") == "video":
                print(f"  [{m['y0']:6d}] {m['vstem'][:10]} conf={m['vconf']} "
                      f"{m.get('vdur','?')} {m.get('vsize','')} "
                      f"{'已下载' if m.get('vpath') else '未下载'}")
