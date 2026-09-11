#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取+解析引擎：CLI 和桌面 App 共用。"""
import os, sys, time, subprocess
from PIL import Image
from wechat_ui import (WeChatView, ensure_front, frontmost_name, match_shift,
                       check_stop, StopRequested, FocusLost, TMP_DIR)
from geometry import capture_window
from ocr import ocr_image
from parse3 import parse_image


def preflight():
    """返回 (ok, message)。"""
    # 用户可能把导出/临时目录删了或挪走(打包版这些目录在 ~/Documents 下，
    # 是用户随手能动的)，那样后续截图会 Errno 2。每次开抓前补建一次。
    os.makedirs(TMP_DIR, exist_ok=True)
    try:
        subprocess.check_output(["pgrep", "-x", "WeChat"])
    except subprocess.CalledProcessError:
        return False, "没检测到微信在运行，请先打开微信并登录，再打开要导出的会话。"
    import ApplicationServices as AX
    if not AX.AXIsProcessTrusted():
        AX.AXIsProcessTrustedWithOptions({AX.kAXTrustedCheckOptionPrompt: True})
        return False, "缺少【辅助功能】权限：系统设置→隐私与安全性→辅助功能，勾选运行本程序的 App 后重试。"
    test = os.path.join(TMP_DIR, "_pf.png")
    r = subprocess.run(["screencapture", "-x", "-t", "png", test],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        return False, "缺少【屏幕录制】权限：系统设置→隐私与安全性→屏幕录制，勾选运行本程序的 App 后重试。"
    if os.path.exists(test):
        os.remove(test)
    return True, "ok"


def detect_title(v):
    from wechat_ui import _TMP
    capture_window(v.win["id"], _TMP)
    im = Image.open(_TMP)
    s = v.scale
    x0 = v.reg["pane_x_px"] + int(10 * s)
    x1 = v.reg["pane_x_px"] + int((v.reg["W"] - v.reg["pane_x_px"]) * 0.62)
    y0 = int(8 * s); y1 = v.reg["top_px"] - int(6 * s)
    if y1 <= y0 + 10:
        return None
    crop = im.crop((x0, y0, x1, y1))
    tmp = os.path.join(TMP_DIR, "_title.png"); crop.save(tmp)
    lines, _, _ = ocr_image(tmp)
    if os.path.exists(tmp):
        os.remove(tmp)
    lines = [l for l in lines if l["text"].strip()]
    if not lines:
        return None
    # 会话名是这块区域里最靠上、最靠左的那行。不能按"最宽"挑：顶栏下沿如果正好
    # 露出一条居中的系统消息("XXX 撤回了一条消息")，它比会话名更宽，实测会
    # 被当成会话名写进文件名。
    lines.sort(key=lambda l: l["y"])
    band = [l for l in lines if l["y"] - lines[0]["y"] < 20 * s]
    band.sort(key=lambda l: l["x"])
    return band[0]["text"].strip()


def bottom_fingerprint(v, progress=print, max_rounds=60):
    """开抓前给「会话最新一屏」拍个指纹，供后面判断是不是真的抓到底了。

    微信按块加载历史，向下抓的途中会遇到「假底部」：画面卡住不动，等一会儿
    才把下一块加载出来。靠「连续多少轮没动」来判到底，本质是在赌等待时长——
    每轮变快之后实际等待秒数就跟着缩水，实测同一个会话因此少抓了 43 条
    (159 → 116，结尾停在昨天 15:36 而不是最新)。
    有了这张指纹就不用赌：画面和它对上才是真到底，对不上就继续等。
    先向下滚到会话末尾再拍——用户可能停在历史中间(比如从搜索结果跳进来的)。
    往「更新」的方向不存在分块加载(最新的消息本来就在内存里)，所以停住 2.5 秒
    还不动就可以认定是真末尾，不用像向上翻历史那样苦等。
    实在确认不了就返回 None，退回按时长兜底(结果一样完整，只是结尾要多等 40 秒)。"""
    _, g = v.grab()
    for i in range(max_rounds):
        check_stop()
        _, g2 = v.scroll_settled(-3, steps=4, before=g)
        d, score = match_shift(g2, g)
        g = g2
        if score >= 0.5 and d < 8:
            time.sleep(2.5)
            _, g3 = v.scroll_settled(-3, steps=4, before=g)
            d2, s2 = match_shift(g3, g)
            if s2 >= 0.5 and d2 < 8:
                progress(f"  · 已到会话末尾并记下指纹(下滚 {i + 1} 屏)，"
                         "抓到这里就收工")
                return g3
            g = g3
    progress("  · 没能确认会话末尾，改用等待时长判断到底(结尾会多等一会)")
    return None


def scroll_to_top(v, max_steps=400, progress=print):
    """向上滚到会话最开头。微信按块加载历史，滚到已加载块的顶部会先"卡住"，
    等一会儿新块加载完又能继续，所以连续多轮画面不动才算真到顶。"""
    _, prev = v.grab()
    H = prev.shape[0]
    still = 0
    steps = 3                          # 同 stitch_down：上限 4，超过就对不上位
    for i in range(max_steps):
        check_stop()
        _, gray = v.scroll_settled(3, steps=steps, before=prev)  # 正数=向上
        d, score = match_shift(prev, gray)     # 内容向下移动了多少
        if score >= 0.5 and d >= 8:
            frac = d / max(H, 1)
            if score >= 0.85 and frac < 0.55:
                steps = min(4, steps + 1)
            elif score < 0.70 or frac > 0.68:
                steps = max(1, steps - 1)
        if score >= 0.5 and d < 8:
            steps = 3
            still += 1
            if still >= 8:
                # 别急着收：微信到达已加载块的顶部后要去拉更早的历史，慢的时候
                # 能停十几秒。多等一次再试两下，两下都不动才算真的到开头，
                # 否则会把"正在加载"当成"没有更早的消息"，导出少一大截。
                time.sleep(2.5)
                moved = False
                for _ in range(2):
                    check_stop()
                    _, g2 = v.scroll_settled(3, steps=3, before=prev)
                    d2, s2 = match_shift(prev, g2)
                    prev = g2
                    if not (s2 >= 0.5 and d2 < 8):
                        moved = True
                        break
                if not moved:
                    progress(f"  · 已到会话开头(第 {i+1} 轮)")
                    return i + 1
                still = 0
                steps = 3
                continue
            time.sleep(0.9)            # 给分块加载留时间
        else:
            still = 0
            if i % 10 == 0:
                progress(f"  ...向上翻 {i} 屏")
        prev = gray
    progress(f"  ! 翻了 {max_steps} 屏仍未到顶，从这里开始抓")
    return max_steps


def stitch_down(v, max_steps=500, progress=print, on_frame=None, end_fp=None):
    """从当前位置向下滚动拼整段(当前顶部→最新)。on_frame(v) 每帧后执行(如语音转文字)，
    返回是否需要重抓。向下滚时内容向上移动，新内容出现在底部，逐段贴到画布下方。

    微信从「搜索聊天记录」跳到历史位置后消息是分块加载的：滚到已加载块的假底部会
    先停滞，随后加载完成、视图被自动往上重新锚定(用户看到的"自动上翻")。因此按
    匹配分数区分三种帧，绝不把假底部当成真到底：
    - 高分且位移>=8 → 正常前进，拼接；
    - 高分且无位移 → 停滞，多等几轮再认定真到底(假底部通常一两秒内就会跳块)；
    - 低分 → 视图被微信挪走了，不拼接，只向下滚动追赶，直到重新对上拼接位置。"""
    SCORE_OK = 0.5
    rgb, gray = v.grab()
    canvas = rgb
    H = gray.shape[0]
    # 起始帧也要处理语音(顶部这一屏的语音气泡)
    if on_frame is not None and on_frame(v):
        rgb, gray = v.grab(); canvas = rgb
    prev_gray = gray
    still = 0     # 连续停滞轮数(高分、无位移)
    seek = 0      # 视图跳到上方后向下追赶的轮数(反向匹配成功)
    lost = 0      # 与上次拼接位置完全对不上的轮数(双向都低分)
    rebases = 0   # 放弃对位、直接续接的次数
    # 每轮滚多少格滚轮。实测标定(帧高 1129px)：
    #   steps=1→180px  2→360px  3→540px  4→720px 分数都是 1.000
    #   steps=6→712px 分数 0.238   steps=8→800px 分数 0.155
    # 也就是说超过 4 格之后位移不再增加、画面却对不上了(滚太急，截到了动画中间帧)，
    # 所以上限锁死在 4。以前这里硬编码从 6 起步，等于每次都从"对不上"开始，
    # 实测导致整段只抓到一屏就误判到底。
    steps = 3
    stall_confirm = 0
    stall_t0 = None       # 本轮停滞是从什么时候开始的(按时长而不是轮数判到底)
    for i in range(max_steps):
        check_stop()
        rgb, gray = v.scroll_settled(-3, steps=steps, before=prev_gray)  # 向下
        if on_frame is not None and on_frame(v):
            rgb, gray = v.grab()
        # 内容向上移动了 d 像素：match_shift(cur, prev) 得到该上移量
        d, score = match_shift(gray, prev_gray)
        if score >= SCORE_OK and d >= 8:
            still = seek = lost = 0
            stall_confirm = 0
            stall_t0 = None
            frac = d / max(H, 1)
            # 按「实际位移 + 匹配质量」双指标调步长：分数高说明帧间对位很稳，
            # 可以多滚一点少跑几轮；分数一掉就立刻收——对不上位要走跳变恢复，
            # 既慢又可能在接缝处丢消息。实测 steps=4 时最低分 0.678，余量偏薄，
            # 所以只在分数足够漂亮时才放到 4。
            if score >= 0.85 and frac < 0.55:
                steps = min(4, steps + 1)
            elif score < 0.70 or frac > 0.68:
                steps = max(1, steps - 1)
            d = min(d, H)
            new_bottom = rgb.crop((0, rgb.height - d, rgb.width, rgb.height))
            merged = Image.new("RGB", (canvas.width, canvas.height + d))
            merged.paste(canvas, (0, 0)); merged.paste(new_bottom, (0, canvas.height))
            canvas = merged
            prev_gray = gray
            if i % 8 == 0:
                progress(f"  ...已抓取 {canvas.height}px")
            continue
        if score >= SCORE_OK:
            # 高分停滞：真到底，或分块加载中的假底部(随后跳块会落到下面的分支)
            seek = lost = 0
            steps = 3
            # 有末尾指纹就直接问「这是不是最新那一屏」，是就立刻收工，
            # 不用为了保险白等十几秒；不是就说明还有内容，继续耐心等加载。
            if end_fp is not None and canvas.height > H:
                de, se = match_shift(gray, end_fp)
                if se >= 0.5 and de < 8:
                    progress("  · 画面已与会话末尾一致，抓取完成")
                    break
            if stall_t0 is None:
                stall_t0 = time.time()
            waited = time.time() - stall_t0
            # 一条都还没拼进来就「不动」，几乎肯定不是到底了：刚从会话顶部回来时
            # 微信正在加载最早那批历史，这期间滚轮完全不响应(实测锁死 40 秒)。
            # 此时必须给足耐心，否则整段一个像素都抓不到。开始拼图之后就不用这么等。
            patience = 12.0 if canvas.height > H else 60.0
            if waited >= patience:
                if stall_confirm < 2:
                    stall_confirm += 1
                    stall_t0 = time.time()
                    time.sleep(2.5)
                    continue
                progress(f"  ! 连续 {patience * 3 / 60:.0f} 分钟无法继续向下，"
                         "提前结束；结果可能不完整")
                break
            if waited > 3.0 and int(waited) % 6 == 0:
                progress(f"  · 微信正在加载(已等 {waited:.0f}s)，继续等...")
            time.sleep(0.7)
            continue
        still = 0
        dr, sr = match_shift(prev_gray, gray)
        if sr >= SCORE_OK and dr >= 8:
            # 视图在拼接位置上方且内容有重叠(典型"自动上翻")：向下追赶即可重新对上
            seek += 1; lost = 0
            if seek == 1:
                progress("  · 微信视图自动上翻(历史分块加载)，正在向下重新对位...")
            if seek < 40:
                continue
        else:
            # 双向都对不上：跳变后微信重排了版面，或者落点直接跳过了原来那一屏。
            lost += 1
            if lost == 1 and seek == 0:
                progress("  · 微信视图自动跳动(历史分块加载)，尝试重新对位...")
            if lost < 10:
                # 关键：跳变可能把视图甩到拼接位置的【下方】——中间那段被跳过了。
                # 这时继续闷头往下滚永远追不回来，那段内容就永久丢失(实测两处接缝
                # 各吃掉一条消息)。所以往回滚一屏，把重叠区找回来再对位。
                v.scroll(3, steps=3)
                time.sleep(0.8)
                steps = 2          # 重新对上之前小步走，别再跨过去
                continue
        # 追不上/对不上：放弃精确对位，从当前位置直接续接。
        # 画一条背景色分隔带作接缝标记；宁可少量重复或缺失，也不整段丢弃
        rebases += 1
        if rebases > 20:
            progress("  ! 视图跳变过于频繁，提前结束；结果可能不完整")
            break
        progress("  · 无法精确对位，从当前位置续接(接缝处可能有少量重复或缺失)")
        bg = rgb.crop((0, 0, rgb.width, 4)).resize((1, 1), Image.LANCZOS).getpixel((0, 0))
        merged = Image.new("RGB", (canvas.width, canvas.height + 30 + rgb.height), bg)
        merged.paste(canvas, (0, 0)); merged.paste(rgb, (0, canvas.height + 30))
        canvas = merged
        prev_gray = gray
        still = seek = lost = 0
        steps = 2         # 续接后小步起步，降低再次跨过去的概率
        time.sleep(1.8)   # 让微信把分块加载/重排安顿下来，避免连环跳变
    return canvas


def capture_and_parse(max_steps=500, do_voice=False, progress=print, stitched_out=None,
                      from_top=False):
    # 每行进度带上已用秒数，人工操作时一眼看出慢在哪一步(滚动/OCR/渲染)
    _t0 = time.time()
    _raw = progress

    def progress(m):
        _raw(f"[{time.time() - _t0:5.1f}s] {m}")

    ok, msg = preflight()
    if not ok:
        raise RuntimeError(msg)
    if not ensure_front():
        raise RuntimeError(f"无法把微信切到前台(当前:{frontmost_name()})，请手动点开微信主窗口后重试")
    v = WeChatView(); v.refresh_geometry()
    title = detect_title(v) or "微信会话"
    progress(f"· 会话: {title}")

    # 语音转文字用「两趟」：先从当前位置滚到底把语音全部转完(转写永久留在微信里)，
    # 回到起点上方，再干净地向下拼图。绝不能边滚边转——转写文字在气泡下方、常已滚出当前帧。
    n_voice = 0
    try:
        # 先把消息区下边界定准，再拍末尾指纹——两者必须用同一套裁剪范围，
        # 否则指纹和后面的帧尺寸对不上，判「到底」就不准了。
        v.calibrate_bottom(progress=progress)
        end_fp = None
        if from_top:
            end_fp = bottom_fingerprint(v, progress=progress)
            progress("· 先向上滚到会话开头(请勿操作鼠标键盘；急停 ⌃⌥⌘+.)...")
            scroll_to_top(v, max_steps, progress=progress)
            # 这里不再 refresh_geometry()：窗口没动，几何是稳定的，
            # 重测反而会把上面校准掉的输入框/草稿又框回来。
            time.sleep(1.5)
        if do_voice:
            from voice import transcribe_down
            progress("· 语音自动转文字：从当前位置向下逐屏转写(较慢；急停 ⌃⌥⌘+.)...")
            steps, n_voice = transcribe_down(v, progress=progress, max_steps=max_steps)
            progress(f"· 语音转写完成，共 {n_voice} 条；回到起点准备抓取...")
            # 用与下行相同的步长(2)回滚，再加余量补偿转写后长出来的文字高度；
            # 多回滚到起点上方，多抓的顶部可在预览里裁掉
            for _ in range(steps + 8):
                check_stop(); v.scroll(2)
            # 大幅回滚后微信的历史分块还在加载，立即下滚会触发重新锚定跳变；多等一会
            time.sleep(1.8)

        progress("· 从当前顶部位置向下抓取到最新(请勿操作鼠标键盘；急停 ⌃⌥⌘+.)...")
        canvas = stitch_down(v, max_steps, progress=progress, end_fp=end_fp)
    except (StopRequested, FocusLost) as e:
        raise RuntimeError(str(e))
    stitched_path = stitched_out or os.path.join(TMP_DIR, "stitched_full.png")
    canvas.save(stitched_path)
    progress("· OCR + 解析中...")
    im, msgs = parse_image(stitched_path)
    n_msg = sum(1 for m in msgs if m["type"] != "time")
    progress(f"· 解析出 {n_msg} 条消息" + (f"，语音转写 {n_voice} 条" if do_voice else ""))
    return {"im": im, "msgs": msgs, "title": title, "scale": v.scale,
            "stitched_path": stitched_path}
