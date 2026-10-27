"""普通附件补下载：只操作已核对文件名的卡片，完成后交给原导出器复制。"""
import os
import re
import tempfile
import time
import uuid

from wxfiles import FileIndex, _norm, download_complete, human_size
from wechat_ui import StopRequested, FocusLost, check_stop, ensure_front_or_raise, TMP_DIR


def _attach(message, entry, confidence=1.0):
    message["fpath"] = entry["path"]
    message["fconf"] = confidence
    message["fsize_real"] = human_size(os.path.getsize(entry["path"]))
    message.pop("fdownload_error", None)
    message.pop("fnote", None)


def _lookup(message, index_factory):
    path = message.get("fdownload_path")
    if path and os.path.isfile(path):
        return {"path": path, "name": os.path.basename(path)}, 1.0
    return index_factory().find(message.get("fname", ""))


def download_attachments(v, image, msgs, progress=print, timeout=180,
                         poll_interval=0.7, settle=2.0, ui=None, index_factory=None):
    """逐项补下载，返回(已在本机完整落盘数, 文件消息数)。失败原因留在每条消息。"""
    factory = index_factory or FileIndex
    files = [m for m in msgs if m.get("type") == "file"]
    pending = []
    for message in files:
        entry, confidence = _lookup(message, factory)
        if entry and download_complete(entry["path"], message.get("fsize", "")):
            _attach(message, entry, confidence)
        else:
            message.pop("fpath", None)
            message.pop("fcopy", None)
            pending.append(message)
    if not pending:
        return len(files), len(files)

    adapter = ui or WeChatAttachmentUI(v, image)
    progress(f"· {len(pending)} 个附件尚未完整下载，开始让微信逐个下载…")
    try:
        adapter.prepare()
        for number, message in enumerate(sorted(pending, key=lambda m: m.get("y0", 0), reverse=True), 1):
            check_stop()
            name = message.get("fname") or "未识别文件名"
            # 下载开始前再看一次：上一条下载期间，微信可能已自行完成了本条。
            entry, confidence = _lookup(message, factory)
            if entry and download_complete(entry["path"], message.get("fsize", "")):
                _attach(message, entry, confidence)
                continue
            if re.search(r"过期|失效", message.get("fstatus", "")):
                message["fdownload_error"] = "微信显示文件已过期或失效，无法下载"
                progress(f"  · 未完成 {name}：{message['fdownload_error']}")
                continue
            progress(f"  · 补下载附件 {number}/{len(pending)}：{name}")
            try:
                location = adapter.locate(message)
                if location is None:
                    message["fdownload_error"] = "未能在当前微信会话中确认这张文件卡片，未点击"
                    continue
                # 只下载本机缺失的附件，不双击或执行已经下载的文件。
                if not entry or os.path.getsize(entry["path"]) == 0:
                    adapter.activate_download(message, location)
                started = time.monotonic()
                signature = None
                stable_since = None
                while time.monotonic() - started < timeout:
                    check_stop()
                    problem = adapter.problem(message, location)
                    if problem:
                        message["fdownload_error"] = problem
                        break
                    entry, confidence = _lookup(message, factory)
                    if entry:
                        stat = os.stat(entry["path"])
                        current = (entry["path"], stat.st_size, stat.st_mtime_ns)
                        complete = download_complete(entry["path"], message.get("fsize", ""))
                        if current == signature and complete:
                            if stable_since is None:
                                stable_since = time.monotonic()
                            if time.monotonic() - stable_since >= settle:
                                _attach(message, entry, confidence)
                                progress(f"  · 已下载 {name}（{human_size(stat.st_size)}）")
                                break
                        else:
                            signature = current
                            stable_since = None
                    time.sleep(poll_interval)
                else:
                    message["fdownload_error"] = "下载超时，未确认文件完整落盘；可在预览中重试"
            except (StopRequested, FocusLost) as error:
                message["fdownload_error"] = "附件下载已中断：" + str(error)
                raise
            except Exception as error:
                message["fdownload_error"] = "下载未完成：" + str(error)
            finally:
                if not message.get("fpath"):
                    message.setdefault("fdownload_error", "未确认文件完整下载")
                    progress(f"  · 未完成 {name}：{message['fdownload_error']}")
                adapter.finish_file()
    except (StopRequested, FocusLost) as error:
        for message in pending:
            if not message.get("fpath"):
                message.setdefault("fdownload_error", "附件下载已中断：" + str(error))
        raise
    except Exception as error:
        for message in pending:
            if not message.get("fpath"):
                message.setdefault("fdownload_error", "无法开始附件下载：" + str(error))
        progress("· 附件补下载未完成：" + str(error))
    finally:
        adapter.restore()
    completed = sum(bool(m.get("fpath")) for m in files)
    progress(f"· 附件下载核对：{completed}/{len(files)} 个已在本机，{len(files) - completed} 个未完成")
    return completed, len(files)


def _ax_value(element, attribute):
    import ApplicationServices as AX
    error, value = AX.AXUIElementCopyAttributeValue(element, attribute, None)
    return value if error == 0 else None


def _ax_nodes(pid):
    """只读取微信的原生菜单/保存面板；Qt聊天正文不依赖辅助功能树。"""
    import ApplicationServices as AX
    queue = [(AX.AXUIElementCreateApplication(pid), 0)]
    count = 0
    while queue and count < 600:
        element, depth = queue.pop(0)
        count += 1
        yield element
        import ApplicationServices as AX
        # 文件右键菜单独立于菜单栏；不遍历菜单栏里的隐藏菜单，避免误点其它下载项。
        if depth < 9 and _ax_value(element, AX.kAXRoleAttribute) != "AXMenuBar":
            queue.extend((child, depth + 1) for child in (_ax_value(element, AX.kAXChildrenAttribute) or []))


def _ax_named(pid, role, names=(), identifiers=()):
    import ApplicationServices as AX
    for element in _ax_nodes(pid):
        if _ax_value(element, AX.kAXRoleAttribute) != role:
            continue
        title = _ax_value(element, AX.kAXTitleAttribute) or ""
        description = _ax_value(element, AX.kAXDescriptionAttribute) or ""
        identifier = _ax_value(element, "AXIdentifier") or ""
        if title in names or description in names or identifier in identifiers:
            return element
    return None


def _key(code, command=False, shift=False):
    import Quartz
    flags = (Quartz.kCGEventFlagMaskCommand if command else 0) | (Quartz.kCGEventFlagMaskShift if shift else 0)
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, code, down)
        Quartz.CGEventSetFlags(event, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.04)


class WeChatAttachmentUI:
    def __init__(self, view, image):
        from wximage import _ref
        self.view = view
        self.ref, self.row_pixels = _ref(image)
        height = view.reg["bottom_px"] - view.reg["top_px"]
        self.rounds = max(20, int(image.height / max(height * 0.3, 1)) + 20)
        self.start_top = None
        self.created_save_panel = False

    def prepare(self):
        from wximage import _locate, LOCATE_MIN
        ensure_front_or_raise()
        top, score = _locate(self.view, self.ref, self.row_pixels)
        if top is None or score < LOCATE_MIN:
            raise RuntimeError("当前微信画面与本次抓取不一致，请打开对应会话后重试")
        self.start_top = top

    def _seek(self, row, box=None):
        from wximage import _locate, LOCATE_MIN
        from wechat_ui import match_shift
        view = self.view
        height = view.reg["bottom_px"] - view.reg["top_px"]
        still = 0
        for _ in range(self.rounds):
            check_stop()
            ensure_front_or_raise()
            top, score = _locate(view, self.ref, self.row_pixels)
            if top is None or score < LOCATE_MIN:
                return None  # 对不上本次长图时绝不盲点、盲翻其他聊天。
            if box is None and abs(top - row) < height * 0.15:
                return top
            if box is not None and box[0] - top >= 8 and box[1] - top <= height - 8:
                return top
            target = row if box is None else row - height / 2
            distance = target - top
            if box is not None and abs(distance) < 8:
                return None
            _, before = view.grab()
            _, after = view.scroll_settled(-3 if distance > 0 else 3,
                                           steps=1 if abs(distance) < height * 0.6 else 3,
                                           before=before)
            moved, quality = match_shift(before, after) if distance < 0 else match_shift(after, before)
            still = still + 1 if quality >= 0.5 and moved < 8 else 0
            if still >= 6:
                return None
        return None

    def _card(self, message, top):
        from ocr import ocr_image
        from parse3 import take_file_card
        rgb, _ = self.view.grab()
        bounds = (max(0, message["x0"] - 8), max(0, int(message["y0"] - top) - 8),
                  min(rgb.width, message["x1"] + 8), min(rgb.height, int(message["y1"] - top) + 8))
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            return None
        with tempfile.TemporaryDirectory(prefix="wechat-attachment-card-") as directory:
            path = os.path.join(directory, "card.png")
            rgb.crop(bounds).save(path)
            rows, _, _ = ocr_image(path)
        return take_file_card(rows)

    def locate(self, message):
        if not all(key in message for key in ("x0", "x1", "y0", "y1")):
            return None
        top = self._seek((message["y0"] + message["y1"]) / 2, (message["y0"], message["y1"]))
        if top is None:
            return None
        card = self._card(message, top)
        if not card or _norm(card["name"], True) != _norm(message.get("fname", ""), True):
            return None
        return top

    def activate_download(self, message, top):
        import ApplicationServices as AX
        import Quartz
        from wechat_ui import move_mouse
        ensure_front_or_raise()
        # 再次读取同一张卡片，防止滚动/重绘后沿用旧坐标。
        card = self._card(message, top)
        if not card or _norm(card["name"], True) != _norm(message.get("fname", ""), True):
            raise RuntimeError("文件卡片已变化，未点击")
        if re.search(r"过期|失效", card.get("status", "")):
            raise RuntimeError("微信显示文件已过期或失效")
        view = self.view
        gx = view.win["x"] + (view.reg["pane_x_px"] + (message["x0"] + message["x1"]) / 2) / view.scale
        gy = view.win["y"] + (view.reg["top_px"] + (message["y0"] + message["y1"]) / 2 - top) / view.scale
        move_mouse(gx, gy)
        for event_type in (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp):
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(
                None, event_type, Quartz.CGPointMake(gx, gy), Quartz.kCGMouseButtonRight))
            time.sleep(0.08)
        time.sleep(0.25)
        download = _ax_named(view.pid, "AXMenuItem", ("下载", "下載", "下载文件", "Download"))
        if download is not None:
            if AX.AXUIElementPerformAction(download, AX.kAXPressAction) != 0:
                raise RuntimeError("无法点击微信的下载菜单")
            return
        save = _ax_named(view.pid, "AXMenuItem", ("另存为…", "另存为...", "另存为", "另存為…", "Save As…", "Save As..."))
        if save is not None:
            if AX.AXUIElementPerformAction(save, AX.kAXPressAction) != 0:
                raise RuntimeError("无法打开微信的另存为面板")
            self.created_save_panel = True
            self._save_as(message)
            return
        _key(53)
        raise RuntimeError("微信未提供可确认的下载/另存为菜单，未打开或运行附件")

    def _wait_control(self, role, names=(), identifiers=(), timeout=5):
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            check_stop()
            found = _ax_named(self.view.pid, role, names, identifiers)
            if found is not None:
                return found
            time.sleep(0.1)
        raise RuntimeError("微信未出现可操作的保存面板")

    def _save_as(self, message):
        import ApplicationServices as AX
        self._wait_control("AXButton", ("存储", "保存", "Save"))
        from appdirs_local import data_root
        directory = os.path.join(data_root(), "已下载附件", uuid.uuid4().hex)
        os.makedirs(directory, exist_ok=True)
        _key(5, command=True, shift=True)  # 系统保存面板的前往文件夹。
        path_field = self._wait_control("AXTextField", identifiers=("PathTextField",))
        if AX.AXUIElementSetAttributeValue(path_field, AX.kAXValueAttribute, directory) != 0:
            raise RuntimeError("无法选择附件下载目录")
        _key(36)
        name_field = self._wait_control("AXTextField", ("存储为：", "存储为", "Save As:"),
                                        ("saveAsNameTextField", "NSSavePanelNameField"))
        name = os.path.basename(message["fname"]).replace("\x00", "").strip()
        if not name or name in (".", ".."):
            raise RuntimeError("附件文件名无效")
        if AX.AXUIElementSetAttributeValue(name_field, AX.kAXValueAttribute, name) != 0:
            raise RuntimeError("无法填写附件文件名")
        save = self._wait_control("AXButton", ("存储", "保存", "Save"))
        message["fdownload_path"] = os.path.join(directory, name)
        if AX.AXUIElementPerformAction(save, AX.kAXPressAction) != 0:
            raise RuntimeError("无法确认附件下载位置")
        self.created_save_panel = False

    def problem(self, message, top):
        import Quartz
        current = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionIncludingWindow, self.view.win["id"]) or []
        if not any(w.get("kCGWindowNumber") == self.view.win["id"] and w.get("kCGWindowIsOnscreen") for w in current):
            raise FocusLost("微信聊天窗口已关闭或登录状态发生变化")
        card = self._card(message, top)
        if card and re.search(r"过期|失效", card.get("status", "")):
            return "微信显示文件已过期或失效，无法下载"
        return None

    def finish_file(self):
        if self.created_save_panel:
            _key(53)
            self.created_save_panel = False

    def restore(self):
        if self.start_top is None:
            return
        try:
            check_stop()
            self._seek(self.start_top)
        except (StopRequested, FocusLost, RuntimeError):
            pass
