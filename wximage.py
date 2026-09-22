#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""聊天里的图片：让微信自己吐出原图，代替气泡截图。

气泡里那张是缩略图。微信给图片气泡限宽约 160pt，Retina 下截出来也就 320px
见方——这就是导出的图「特别模糊」的全部原因，不是渲染或缩放写错了。

原图在容器里是加密的(WeChat 4.x 的 .dat 以 07 08 56 32 "\\x07\\x08V2" 开头)，
但不用碰解密：微信自己留了一条明路——在「图片和视频」预览窗里按 ⌘C，它会把
解好的 PNG 写到
    ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/
      xwechat_files/<wxid>/temp/RWTemp/YYYY-MM/<哈希>.png
并把这个文件的 public.file-url 放上剪贴板。所以不用去猜目录、不用扫盘，
按完 ⌘C 直接问剪贴板要路径即可。实测：气泡 319x319 → 原图 1623x1133。

只需要点开【一张】。预览窗的 ← → 是在会话的图片序列里走，所以点开一个认得出
的气泡之后，从它出发往两头走、每站按一次 ⌘C 就行，不用逐个气泡去点(逐个点
要反复滚动+对位，慢一个数量级)。

但【不能闷头走到头】：预览序列是整个会话的历史，往往远多于这次要导出的这段。
实测一个工作群，这次导出只涉及 20 张图，序列里却有 600+ 张——先"退到第一张"
就白走了 200 步、192 秒。所以走法是：每站当场跟还没对上号的气泡比对，连续
若干站都对不上就收手，两头都这么干，再加一个步数上限兜底。

认哪张原图对应哪条消息，靠图像比对(NCC)加位置线索：预览窗的 ←/→ 走的就是
会话顺序，所以过线的候选里挑离"该轮到谁"最近的那个。只按相似度取最高分会
张冠李戴——同一个后台的两张截图实测能到 0.9x。
"""
import os, shutil, time
import numpy as np
from PIL import Image

# 下面这几个是视频那边写好的同一套东西(同一个微信、同一套窗口枚举和事件注入)，
# 直接复用，不另起一份。
from wxvideo import (_prep, _slide, _click, _key, _sub_windows, _close_preview,
                     media_blocks, human_size)

MATCH_MIN = 0.88        # 认定「原图和这个气泡是同一张」的最低分
KEY_LEFT, KEY_RIGHT, KEY_C = 123, 124, 8
MISS_STOP = 6           # 连续这么多站对不上号就收手
MAX_EMBED_PX = 3000     # 塞进 Word 的图最长边上限，再大只是把文档撑肥
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".heic", ".tiff")


def orig_dir():
    """原图的中转站。抓取时先落在这儿，导出时再按选区复制进导出文件夹。

    不直接引用 RWTemp 里那份：那是微信的临时目录，它什么时候清不归我们管。"""
    from wechat_ui import TMP_DIR
    return os.path.join(TMP_DIR, "原图")


# ---------------------------------------------------------------- 剪贴板
def pb_backup():
    """扫一遍图要按几十次 ⌘C，会把用户剪贴板里的东西冲掉，先存一份。"""
    try:
        from AppKit import NSPasteboard
        items = []
        for it in (NSPasteboard.generalPasteboard().pasteboardItems() or []):
            d = {}
            for t in it.types():
                data = it.dataForType_(t)
                if data is not None:
                    d[str(t)] = bytes(data)
            if d:
                items.append(d)
        return items
    except Exception:
        return None


def pb_restore(items):
    try:
        from AppKit import NSPasteboard, NSPasteboardItem
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        if not items:
            return
        objs = []
        for d in items:
            it = NSPasteboardItem.alloc().init()
            for t, data in d.items():
                it.setData_forType_(data, t)
            objs.append(it)
        pb.writeObjects_(objs)
    except Exception:
        pass


def _copy_here(timeout=4.0):
    """在预览窗里按一次 ⌘C，返回微信吐出来的那个文件路径(没吐出来返回 None)。

    先清空剪贴板再按：这样「剪贴板上出现了 file-url」就等于「这次按成了」，
    不会把上一张的残留当成这一张。"""
    try:
        from AppKit import NSPasteboard
    except Exception:
        return None
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    _key(KEY_C, cmd=True)
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.12)
        for it in (pb.pasteboardItems() or []):
            d = it.dataForType_("public.file-url")
            if not d:
                continue
            url = bytes(d).decode("utf-8", "ignore")
            p = url.split("://localhost", 1)[-1] if "://" in url else url
            from urllib.parse import unquote
            p = unquote(p)
            if os.path.exists(p) and p.lower().endswith(IMG_EXT):
                return p
            return None      # 吐出来的不是图片(序列里混着视频)，跳过这一站
    return None


# ---------------------------------------------------------------- 预览窗翻页
def _frame(win_id):
    """预览窗当前画面的小灰度指纹。"""
    from wechat_ui import capture_window_rgb
    a = capture_window_rgb(win_id)
    if a is None:
        return None
    return a[::8, ::8].astype(np.float32).mean(axis=2)


def _differ(a, b, tol=0.08):
    if a is None or b is None or a.shape != b.shape:
        return True
    return float(np.abs(a - b).mean()) > tol


def _turn(win_id, code, settle=1.6, stable=True):
    """按一下 ← 或 →。返回是否真的翻动了——没翻动就是走到头了。

    到头时微信是【一点反应都没有】，画面逐像素不动，所以判定阈值可以压得很低。
    翻动之后还要等它把大图解完码再回去按 ⌘C，不然会拿到上一张；只是路过
    (往回退到第一张的那一趟)就不必等，stable=False 每张能省约 1 秒。"""
    before = _frame(win_id)
    _key(code)
    t0 = time.time()
    cur, moved = before, False
    while time.time() - t0 < settle:
        time.sleep(0.12)
        cur = _frame(win_id)
        if _differ(before, cur):
            moved = True
            break
    if not moved or not stable:
        return moved
    prev = cur
    for _ in range(14):
        time.sleep(0.12)
        cur = _frame(win_id)
        if not _differ(prev, cur, tol=0.05):
            break
        prev = cur
    return True


def _preview_win(pid, timeout=6.0):
    """等「图片和视频」预览窗弹出来，返回窗口 id。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        ws = _sub_windows(pid)
        if ws:
            return ws[0]["id"]
        time.sleep(0.2)
    return None


def _score(B, T):
    """气泡 B 和原图 T 像不像。不像/没法比就返回 -1。

    两道闸都是实测踩出来的：一张 493x163 的气泡被配到了 1807x112 的原图上，
    导出来完全是另一张图。
    · 长宽比：微信只会把图【竖着裁】(气泡限高)，绝不会把一张扁图显示得更高。
      所以原图归一化后必须不比气泡矮，矮了就根本不可能是同一张。
    · 重叠行数：_prep 统一缩到 48 宽，高度就是长宽比。上面那张扁原图只剩 3 行，
      拿 3x48=144 个像素比出来的分数纯属噪声，随便哪张都能过 0.88。"""
    hb, ht = B.shape[0], T.shape[0]
    if ht + 1 < hb:
        return -1.0
    if min(hb, ht) < 6:
        return -1.0
    return _slide(B, T, None)


def _match(arr, entries, expected):
    """这一站取到的原图是哪个气泡。过线的候选里挑离 expected 最近的那个——
    预览窗的 ←/→ 走的就是会话顺序，"该轮到谁"本身是很强的线索。"""
    best = None
    for i, e in enumerate(entries):
        if e.get("src"):
            continue
        sc = _score(e["arr"], arr)
        if sc < MATCH_MIN:
            continue
        key = (abs(i - expected), -sc)
        if best is None or key < best[0]:
            best = (key, i, sc)
    return (best[1], best[2]) if best else (None, 0.0)


def _station(win_id, entries, expected):
    """当前这一站：按 ⌘C 把原图要出来，对上号就记在那个气泡名下。"""
    p = _copy_here()
    if not p:
        return None
    try:
        arr = _prep(Image.open(p))
    except Exception:
        return None
    j, sc = _match(arr, entries, expected)
    if j is None:
        return None
    entries[j]["src"] = p
    entries[j]["score"] = sc
    return j


def _walk(win_id, entries, expected, code, progress, miss_stop=MISS_STOP):
    """从当前位置朝一个方向走，每站取原图并当场对号。返回 (走了几步, 取到几张)。

    连续 miss_stop 站对不上号就收手，再加步数上限——两道闸都是为了不在
    「会话里有几百张图、这次只导出其中二十张」时白走。"""
    from wechat_ui import check_stop
    d = -1 if code == KEY_LEFT else 1
    cap = len(entries) + miss_stop + 10
    steps = misses = got = 0
    while steps < cap and misses < miss_stop:
        check_stop()
        if not _turn(win_id, code):
            break                      # 走到序列尽头了
        steps += 1
        j = _station(win_id, entries, expected)
        if j is None:
            misses += 1
            expected += d
        else:
            misses = 0
            got += 1
            expected = j + d
            if got % 5 == 0:
                progress(f"  · 已取出 {sum(1 for e in entries if e.get('src'))}"
                         f"/{len(entries)} 张原图...")
        if all(e.get("src") for e in entries):
            break                      # 都齐了，不用再走
    return steps, got


def sweep(win_id, entries, start, progress=print):
    """从点开的那张出发，先往左走到收手，原路退回，再往右走到收手。"""
    _station(win_id, entries, start)
    back, _ = _walk(win_id, entries, start - 1, KEY_LEFT, progress)
    for _ in range(back):
        _turn(win_id, KEY_RIGHT, stable=False)     # 原路退回起点再往右
    _walk(win_id, entries, start + 1, KEY_RIGHT, progress)
    return sum(1 for e in entries if e.get("src"))


# ---------------------------------------------------------------- 找一张点开
REF_W = 64              # 定位时把长图和当前帧都缩到这个宽度
LOCATE_MIN = 0.55       # 低于这个分就是没对上，别硬点


def _ref(img, width=REF_W):
    """灰度 + 缩到统一宽度，返回 (数组, 每行代表原图多少像素)。"""
    g = img.convert("L")
    h = max(1, int(round(g.height * width / g.width)))
    a = np.asarray(g.resize((width, h), Image.LANCZOS), dtype=np.float32)
    return a, g.height / float(h)


def _locate(v, ref, row_px):
    """当前这一屏是长图的哪一段？返回 (屏幕第一行对应长图的第几行, 分数)。

    早先是"长图最后一行 == 屏幕最后一行，按累计滚动量往回推"。推算会飘：
    实测同一个会话，算出来的气泡落在 y=120 而视口从 128 开始，差 8px 就点空了，
    往回翻之后误差还会越积越大。长图就在手里、当前帧本来就是它的一个切片，
    把帧在长图上滑一遍取最高分，位置是【量】出来的，不累积误差。"""
    rgb, _ = v.grab()
    f, _ = _ref(rgb)
    nf, nr = f.shape[0], ref.shape[0]
    if nf < 4 or nr < nf:
        return None, 0.0
    fz = f - f.mean(axis=None)
    fn = float(np.sqrt((fz * fz).sum()))
    if fn < 1e-6:
        return None, 0.0
    best, bi = -1.0, 0
    for d in range(0, nr - nf + 1):
        w = ref[d:d + nf]
        wz = w - w.mean()
        wn = float(np.sqrt((wz * wz).sum()))
        if wn < 1e-6:
            continue
        sc = float((fz * wz).sum() / (fn * wn))
        if sc > best:
            best, bi = sc, d
    return bi * row_px, best


def _open_one(v, im, entries, progress, max_screens=40, max_tries=3):
    """点开一个图片气泡，返回 (预览窗 id, 这是第几个气泡)。

    每屏都先量一次"现在看到的是长图哪一段"，再挑落在视口里、最靠下的那个
    没试过的气泡点下去。点了不弹预览窗就换下一个(解析出来的 media 块里可能
    混着表情包、链接卡片这种点了没反应的)，最多试 max_tries 个。"""
    from wechat_ui import (wechat_pid, ensure_front_or_raise, check_stop,
                           match_shift)
    pid = wechat_pid()
    ensure_front_or_raise()
    ref, row_px = _ref(im)
    r = v.reg
    view_h = r["bottom_px"] - r["top_px"]
    tried = set()
    lost = 0
    _, prev = v.grab()
    for screen in range(max_screens + 1):
        check_stop()
        top, score = _locate(v, ref, row_px)
        if top is None or score < LOCATE_MIN:
            # 往回翻过头了，翻出了这次抓到的那段。再翻也不会遇到要找的气泡
            lost += 1
            if lost >= 2:
                progress("  · 已翻出本次抓取的范围，不再往回找")
                break
        else:
            lost = 0
        if top is not None and score >= LOCATE_MIN:
            for i in range(len(entries) - 1, -1, -1):     # 从最靠下的挑起
                if i in tried:
                    continue
                m = entries[i]["m"]
                y = (m["y0"] + m["y1"]) / 2.0 - top       # 相对视口顶部
                if y < 30 or y > view_h - 30:
                    continue
                tried.add(i)
                gx = v.win["x"] + (r["pane_x_px"] + (m["x0"] + m["x1"]) / 2.0) / v.scale
                gy = v.win["y"] + (r["top_px"] + y) / v.scale
                # 微信刚切到前台时第一次点击常被窗口激活吃掉(视频那边实测过)
                for _try in range(2):
                    _click(gx, gy)
                    wid = _preview_win(pid, timeout=2.0)
                    if wid:
                        return wid, i
                progress(f"  · 第 {i + 1} 张点下去没弹预览窗，换一张试")
                if len(tried) >= max_tries:
                    return None, None
                break                    # 屏幕可能被动过，重新量一次再挑
        if screen == 0:
            progress(f"  · 这一屏没有能点的图片气泡(当前在长图 {top and int(top)} 行"
                     f"，匹配 {score:.2f})，往回翻找...")
        ensure_front_or_raise()
        _, cur = v.scroll_settled(3, steps=3, before=prev)     # 正数=向上
        d, sc2 = match_shift(prev, cur)
        prev = cur
        if sc2 < 0.5 or d < 8:
            break                        # 翻不动了(到会话开头)
    return None, None


# ---------------------------------------------------------------- 总入口
def collect_originals(v, im, msgs, progress=print, max_total_mb=800):
    """把会话里的图片原图取出来，写进 m["iorig"]。返回 (取到数, 图片消息总数)。"""
    from wechat_ui import wechat_pid
    entries = [{"m": m, "arr": _prep(im.crop((m["x0"], m["y0"], m["x1"], m["y1"])))}
               for m in msgs if m.get("type") == "media"]
    if not entries:
        return 0, 0
    wpx = entries[0]["m"]["x1"] - entries[0]["m"]["x0"]
    progress(f"· 取图片原图：气泡里那张是缩略图(约 {wpx}px 宽)，"
             f"去微信里把 {len(entries)} 张原图要回来...")
    pid = wechat_pid()
    saved = pb_backup()
    dest = orig_dir()
    os.makedirs(dest, exist_ok=True)
    try:
        wid, start = _open_one(v, im, entries, progress)
        if wid is None:
            progress("  ! 没能点开图片预览窗，这次就用气泡截图(会糊一点)")
            return 0, len(entries)
        sweep(wid, entries, start, progress=progress)
    finally:
        if not _close_preview(pid):
            progress("  ! 图片预览窗没关掉，请手动按一下 Esc")
        pb_restore(saved)
        time.sleep(0.3)
    # 取回来的图还在微信的临时目录里，搬进自己的中转站再登记
    hit, total_mb, widest = 0, 0.0, 0
    for e in entries:
        src = e.get("src")
        if not src or not os.path.exists(src):
            continue
        try:
            mb = os.path.getsize(src) / 1024.0 / 1024.0
            if total_mb + mb > max_total_mb:
                progress(f"  ! 原图累计已 {total_mb:.0f}MB，达到上限，剩下的用气泡截图")
                break
            dst = os.path.join(dest, os.path.basename(src))
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
            with Image.open(dst) as pim:
                e["m"]["isize"] = pim.size
                widest = max(widest, pim.width)
            e["m"]["iorig"] = dst
            e["m"]["iconf"] = round(e.get("score", 0.0), 3)
            total_mb += mb
            hit += 1
        except OSError:
            continue
    if hit:
        progress(f"· 原图取到 {hit}/{len(entries)} 张(最宽 {widest}px，共 {total_mb:.1f}MB)"
                 + ("，其余仍用气泡截图" if hit < len(entries) else ""))
    else:
        progress(f"· 一张原图也没取到，{len(entries)} 张图片仍用气泡截图")
    return hit, len(entries)


# ---------------------------------------------------------------- 导出时落地
def save_image(m, media_dir, n_img, im):
    """把这条图片消息写进导出文件夹，返回文件路径。

    有原图就放原图，没有(没取到/关掉了这一步)就退回气泡截图——绝不留空。"""
    src = m.get("iorig")
    if src and os.path.exists(src):
        ext = os.path.splitext(src)[1].lower() or ".png"
        fp = os.path.join(media_dir, f"img_{n_img:03d}{ext}")
        try:
            shutil.copy2(src, fp)
            return fp
        except OSError:
            pass
    fp = os.path.join(media_dir, f"img_{n_img:03d}.png")
    im.crop((m["x0"], m["y0"], m["x1"], m["y1"])).save(fp)
    return fp


def embed_path(fp, max_px=MAX_EMBED_PX):
    """给 Word 用的那份。原图特别大(点过「原图」发的手机照片能到 4000px)时
    先缩一道再塞：按文档里 3.2 英寸的显示宽度算，3000px 已经是 900+ DPI，
    再大只是把 .docx 撑肥，看不出差别。缩出来的临时文件不进导出文件夹。"""
    from wechat_ui import TMP_DIR
    try:
        with Image.open(fp) as pim:
            if max(pim.size) <= max_px:
                return fp
            r = max_px / float(max(pim.size))
            small = pim.convert("RGB").resize(
                (max(1, int(pim.width * r)), max(1, int(pim.height * r))),
                Image.LANCZOS)
            d = os.path.join(TMP_DIR, "embed")
            os.makedirs(d, exist_ok=True)
            out = os.path.join(d, os.path.basename(os.path.splitext(fp)[0]) + ".jpg")
            small.save(out, "JPEG", quality=88)
            return out
    except Exception:
        return fp
