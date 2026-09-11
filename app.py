#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信聊天记录导出 桌面App(pywebview)。抓取→预览→点选起止→导出 Word/Markdown。"""
import os, io, json, time, base64
HERE = os.path.dirname(os.path.abspath(__file__))
# 双击 .app 启动时工作目录是 /(根目录)，相对路径会写到根目录而失败；
# 必须在导入 engine/wechat_ui 之前切到一个确定的目录。
from appdirs_local import data_root, resource_path
try:
    os.chdir(data_root())
except OSError:
    pass          # 目录被删也不致命：程序内部一律用绝对路径
import webview
from engine import capture_and_parse, preflight
from wechat_ui import request_stop, clear_stop
from render_docx import render
from render_md import render_md
from wxfiles import resolve_and_collect
from paths import make_export_dir, layout, OUT_ROOT

OUT_DIR = OUT_ROOT
window = None


class Api:
    def __init__(self):
        self.res = None

    def _log(self, m):
        try:
            window.evaluate_js("window.addLog(%s)" % json.dumps(str(m)))
        except Exception:
            pass

    def check_env(self):
        ok, msg = preflight()
        return {"ok": ok, "msg": msg}

    def stop(self):
        """停止按钮：置软停止标记，抓取循环会尽快中断。"""
        request_stop()
        return True

    def capture(self, max_steps, do_voice, from_top=False):
        def prog(m):
            try:
                window.evaluate_js("window.addLog(%s)" % json.dumps(str(m)))
            except Exception:
                pass
        clear_stop()
        try:
            window.minimize()
        except Exception:
            pass
        try:
            res = capture_and_parse(int(max_steps), bool(do_voice), progress=prog,
                                    from_top=bool(from_top))
        except Exception as e:
            self._to_front()
            return {"ok": False, "msg": str(e)}
        self._to_front()
        self.res = res
        im = res["im"]
        pw = 480
        ratio = pw / im.width
        prev = im.resize((pw, max(1, int(im.height * ratio))))
        buf = io.BytesIO(); prev.save(buf, "PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        msgs = []
        for i, m in enumerate(res["msgs"]):
            msgs.append({
                "i": i, "type": m["type"], "sender": m.get("sender", ""),
                "name": m.get("name", ""),
                "y0": int(m["y0"] * ratio), "y1": int(m["y1"] * ratio),
                "text": (m.get("text") or "")[:60],
            })
        return {"ok": True, "title": res["title"], "img": b64,
                "pw": pw, "ph": prev.height, "messages": msgs}

    def _to_front(self):
        """抓取结束后把 App 从最小化恢复并带回前台(此前微信在前台)。"""
        try:
            window.restore()
        except Exception:
            pass
        try:
            window.on_top = True
            time.sleep(0.4)
            window.on_top = False
        except Exception:
            pass

    def export(self, start_i, end_i, formats, name, want_files=True,
               file_mode="copy"):
        if not self.res:
            return {"ok": False, "msg": "还没有抓取会话"}
        msgs = self.res["msgs"]
        if start_i is None or end_i is None:
            sel = msgs
        else:
            a, b = sorted([int(start_i), int(end_i)])
            sel = msgs[a:b + 1]
        # 去掉选区首尾多余的时间分隔
        while sel and sel[0]["type"] == "time":
            sel = sel[1:]
        while sel and sel[-1]["type"] == "time":
            sel = sel[:-1]
        if not sel:
            return {"ok": False, "msg": "选区为空"}
        fl = [f.lower() for f in (formats or [])]
        if not fl:
            return {"ok": False, "msg": "请至少勾选一种格式"}
        title = (name or "").strip() or self.res["title"]
        im = self.res["im"]; scale = self.res["scale"]
        date = time.strftime("%Y-%m-%d %H:%M")
        # 本次导出独占一个文件夹：文档、图片、文件都放进去，整个夹子可直接归档转发
        d = make_export_dir(title)
        lay = layout(d)
        n_hit = n_file = 0
        missing = []
        if want_files:
            # file_mode="copy" → 把文件复制进本次导出文件夹，文档里用相对路径；
            # file_mode="link" → 不复制，文档里直接写微信本地的原始绝对路径。
            dest = lay["files"] if file_mode == "copy" else None
            try:
                n_hit, n_file = resolve_and_collect(sel, dest)
                missing = [m.get("fname", "") for m in sel
                           if m.get("type") == "file" and not m.get("fpath")]
            except Exception as e:
                self._log("文件收集出错(不影响文档): %s" % e)
        outputs = []
        try:
            if "docx" in fl:
                o, _ = render(im, sel, title, lay["docx"], media_dir=lay["images"],
                              scale=scale); outputs.append(o)
            if "md" in fl:
                o, _ = render_md(im, sel, title, lay["md"], media_dir=lay["images"],
                                 scale=scale, export_date=date); outputs.append(o)
        except Exception as e:
            return {"ok": False, "msg": "导出失败: " + str(e)}
        n_img = len(os.listdir(lay["images"])) if os.path.isdir(lay["images"]) else 0
        return {"ok": True, "outputs": [os.path.basename(o) for o in outputs],
                "folder": d, "folder_name": os.path.basename(d),
                "count": sum(1 for m in sel if m["type"] != "time"),
                "images": n_img, "files": n_hit, "files_total": n_file,
                "copied": file_mode == "copy",
                "missing": missing[:8]}

    def reveal(self, path):
        os.system('open -R "%s"' % path)
        return True

    def open_folder(self, path=None):
        """不传就打开「导出结果」总目录，传了就打开本次导出的那个文件夹。"""
        d = path or OUT_DIR
        os.makedirs(d, exist_ok=True)
        os.system('open "%s"' % d)
        return True


def main():
    global window
    api = Api()
    html = resource_path("ui", "index.html")
    window = webview.create_window("微信聊天记录导出", url=html, js_api=api,
                                   width=620, height=880, min_size=(520, 640))
    webview.start()


if __name__ == "__main__":
    main()
