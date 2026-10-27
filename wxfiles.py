#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在微信本地目录里，按聊天里 OCR 出来的文件名找回真实文件。

微信 4.x 把聊天收到的文件【明文】存在容器里，保留原始文件名，按月分目录：
    ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/
        xwechat_files/<wxid_xxx>/msg/file/YYYY-MM/原文件名.ext
所以不需要解密数据库，只要把 OCR 到的文件名和磁盘上的文件名对上即可。

对不上的常见原因(会如实标注，不猜)：
  · 该文件从没在这台电脑上下载过(没点开过 / 服务器已过期)；
  · OCR 把文件名认错了几个字。
"""
import os, re, unicodedata, zipfile
from difflib import SequenceMatcher
from wxstorage import account_dirs, walk_error

CONTAINER = os.path.expanduser(
    "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files")

# 微信重名时会加 (1)(2) 后缀，匹配时要能忽略
_DUP_RE = re.compile(r'[(（]\d{1,2}[)）]')
# OCR 常见形近误认：字母 O/o↔数字 0、l/I↔1
_OCR_CONFUSE = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1"})


def file_roots():
    """所有已登录账号的 msg/file 目录。"""
    roots = []
    for acc in account_dirs(CONTAINER):
        d = os.path.join(acc, "msg", "file")
        if os.path.isdir(d):
            roots.append(d)
    # 微信“另存为”可直接下载到工具中转目录，恢复抓取时也要能找回它们。
    from appdirs_local import data_root
    staging = os.path.join(data_root(), "_tmp", "附件下载")
    if os.path.isdir(staging):
        roots.append(staging)
    downloads = os.path.join(data_root(), '已下载附件')
    if os.path.isdir(downloads):
        roots.append(downloads)
    return roots


def _norm(name, fold_ocr=False):
    """归一化文件名：全角转半角、去空格、小写、去 (1) 重名后缀。"""
    s = unicodedata.normalize("NFKC", name or "")
    s = _DUP_RE.sub("", s)
    s = re.sub(r'\s+', '', s).lower()
    if fold_ocr:
        s = s.translate(_OCR_CONFUSE)
    return s


class FileIndex:
    """磁盘上所有聊天文件的索引，一次扫描反复查。"""

    def __init__(self, roots=None):
        self.lookup_downloads = roots is None
        self.entries = []          # [{path, name, norm, ocrnorm, mtime, size}]
        self.by_norm = {}
        for root in (roots if roots is not None else file_roots()):
            for dirpath, _dirs, files in os.walk(root, onerror=walk_error):
                for fn in files:
                    if fn.startswith("."):
                        continue
                    fp = os.path.join(dirpath, fn)
                    try:
                        st = os.stat(fp)
                    except OSError:
                        continue
                    e = {"path": fp, "name": fn, "norm": _norm(fn),
                         "ocrnorm": _norm(fn, True),
                         "mtime": st.st_mtime, "size": st.st_size}
                    self.entries.append(e)
                    self.by_norm.setdefault(e["norm"], []).append(e)

    def __len__(self):
        return len(self.entries)

    def find(self, name, min_ratio=0.72):
        """按文件名找回真实文件。返回 (entry, 置信度) 或 (None, 0.0)。

        三档：完全同名 → 1.0；一方是另一方前缀(OCR 把长名截断了) → 0.9；
        再退到编辑距离相似度，低于 min_ratio 就认输、不硬凑。"""
        if not name:
            return None, 0.0
        n = _norm(name)
        if n in self.by_norm:
            # 同名多份(微信的 (1)(2))取最新那份
            return max(self.by_norm[n], key=lambda e: e["mtime"]), 1.0
        if self.lookup_downloads:
            # “另存为”到下载目录的附件不在微信容器里。只核对顶层同名文件，
            # 不递归遍历下载目录里的工程/依赖，也不模糊匹配无关文件。
            try:
                matches = []
                with os.scandir(os.path.expanduser('~/Downloads')) as entries:
                    for item in entries:
                        if _norm(item.name) == n and item.is_file():
                            stat = item.stat()
                            matches.append({'path': item.path, 'name': item.name,
                                            'size': stat.st_size, 'mtime': stat.st_mtime})
                if matches:
                    return max(matches, key=lambda e: e['mtime']), 1.0
            except OSError:
                pass
        no = _norm(name, True)
        ext = os.path.splitext(n)[1]
        best, best_r = None, 0.0
        for e in self.entries:
            if ext and os.path.splitext(e["norm"])[1] != ext:
                continue
            if e["norm"].startswith(n[:-len(ext)] if ext else n) or n.startswith(
                    e["norm"][:-len(ext)] if ext else e["norm"]):
                if 0.9 > best_r:
                    best, best_r = e, 0.9
                continue
            r = SequenceMatcher(None, no, e["ocrnorm"]).ratio()
            if r > best_r:
                best, best_r = e, r
        if best_r >= min_ratio:
            return best, round(best_r, 3)
        return None, 0.0


def human_size(n):
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0


def download_complete(path, size_hint=""):
    """仅用于下载中的附件：大小不能明显不足，ZIP/Office与PDF须有完整结束结构。"""
    try:
        size = os.path.getsize(path)
        if size <= 0:
            return False
        match = re.fullmatch(r"\s*([\d.]+)\s*([KMGB])B?\s*", size_hint or "", re.I)
        if match:
            unit = 1024 ** "B K M G".split().index(match[2].upper())
            # 卡片大小通常保留1位小数；容许半个显示单位的舍入误差。
            tolerance = (0.051 if "." in match[1] else 0.51) * unit
            if size < float(match[1]) * unit - tolerance:
                return False
        ext = os.path.splitext(path)[1].lower()
        if ext in (".zip", ".docx", ".xlsx", ".pptx"):
            with zipfile.ZipFile(path) as archive:
                # 中央目录位于文件尾；不能仅凭暂停增长就把半个ZIP当作下载完成。
                if not archive.infolist():
                    return True
                if any(item.header_offset < 0 or item.header_offset >= size
                       for item in archive.infolist()):
                    return False
        elif ext == ".pdf":
            with open(path, "rb") as stream:
                if not stream.read(8).startswith(b"%PDF-"):
                    return False
                stream.seek(max(0, size - 2048))
                if b"%%EOF" not in stream.read():
                    return False
        return True
    except (OSError, ValueError, zipfile.BadZipFile):
        return False


if __name__ == "__main__":
    import sys
    idx = FileIndex()
    print(f"索引到 {len(idx)} 个微信本地文件，来自:")
    for r in file_roots():
        print("  ", r)
    for q in sys.argv[1:]:
        e, r = idx.find(q)
        print(f"\n查 {q!r}:")
        print(f"  → {e['path']} ({human_size(e['size'])}) 置信度 {r}" if e else "  → 没找到")


def resolve_and_collect(msgs, dest_dir=None, max_copy_mb=None, progress=None):
    """给 type=='file' 的消息补上磁盘上的真实路径；dest_dir 非空则同时复制一份。

    每条会写入：fpath(微信里的原始路径)/fsize_real/fconf(匹配置信度)/
    fcopy(副本相对路径)/fnote(没找到时的说明)。返回 (命中数, 文件消息总数)。"""
    import shutil
    files = [m for m in msgs if m.get("type") == "file"]
    if not files:
        return 0, 0
    say = progress or (lambda *_: None)
    idx = FileIndex()
    say(f"· 在微信本地目录索引到 {len(idx)} 个文件，开始匹配 {len(files)} 个文件卡片...")
    hit = 0
    for m in files:
        for key in ("fpath", "fcopy", "fnote", "fsize_real", "fconf"):
            m.pop(key, None)
        e, conf = idx.find(m.get("fname", ""))
        if not e:
            m["fnote"] = m.get("fdownload_error") or ("微信显示未下载，本机没有这个文件，请先在微信中下载后再导出"
                          if m.get("fstatus") == "未下载"
                          else "本机没有这个文件(没下载过或已过期)")
            continue
        if not download_complete(e["path"], m.get("fsize", "")):
            m["fnote"] = m.get("fdownload_error") or "文件尚未完整下载（大小或文件结构未通过核对）"
            continue
        m.pop("fdownload_error", None)
        m["fpath"] = e["path"]
        m["fsize_real"] = human_size(e["size"])
        m["fconf"] = conf
        if dest_dir:
            if max_copy_mb is not None and e["size"] > max_copy_mb * 1024 * 1024:
                m["fnote"] = f"文件 {human_size(e['size'])} 过大，未复制，见原始位置"
                continue
            os.makedirs(dest_dir, exist_ok=True)
            dst = os.path.join(dest_dir, e["name"])
            n = 1
            base, ext = os.path.splitext(e["name"])
            while os.path.exists(dst) and os.path.getsize(dst) != e["size"]:
                dst = os.path.join(dest_dir, f"{base}_{n}{ext}")
                n += 1
            if not os.path.exists(dst):
                try:
                    shutil.copy2(e["path"], dst)
                except OSError as err:
                    m["fnote"] = f"复制失败: {err}"
                    continue
            m["fcopy"] = os.path.join(os.path.basename(dest_dir), os.path.basename(dst))
        hit += 1
    say(f"· 文件匹配完成：{hit}/{len(files)} 个找到了真实文件")
    return hit, len(files)


def write_attachment_report(msgs, directory, copy_files=True, want_files=True, want_videos=True):
    """逐项列出本次选区的所有文件和视频；不截断失败清单。"""
    lines = ["附件核对清单", ""]
    missing = []
    total = ready = 0
    for message in msgs:
        kind = message.get("type")
        if kind == "file" and want_files:
            name = message.get("fname") or "未识别文件名"
            relative = message.get("fcopy") if copy_files else message.get("fpath")
            note = message.get("fnote") or message.get("fdownload_error") or "文件未下载到本机"
        elif kind == "video" and want_videos:
            name = "视频" + (" " + message["vdur"] if message.get("vdur") else "")
            relative = message.get("vcopy") if copy_files else message.get("vpath")
            note = message.get("vnote") or "视频未下载到本机或未能匹配"
        else:
            continue
        total += 1
        name = str(name).replace("\n", " ")
        path = (os.path.join(directory, relative) if copy_files and relative else relative)
        if relative and os.path.isfile(path):
            ready += 1
            lines.append(f"[已导出] {name}\n  位置：{relative}")
        else:
            missing.append({"name": name, "type": kind, "reason": str(note)})
            lines.append(f"[未完成] {name}\n  原因：{note}")
    if not total:
        return None, missing
    lines[1] = f"本次识别到 {total} 项，已完成 {ready} 项，未完成 {total - ready} 项。"
    path = os.path.join(directory, "附件核对.txt")
    with open(path, "w", encoding="utf-8") as stream:
        stream.write("\n\n".join(lines) + "\n")
    return path, missing
