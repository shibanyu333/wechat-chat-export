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
import os, re, unicodedata
from difflib import SequenceMatcher

CONTAINER = os.path.expanduser(
    "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files")

# 微信重名时会加 (1)(2) 后缀，匹配时要能忽略
_DUP_RE = re.compile(r'[(（]\d{1,2}[)）]')
# OCR 常见形近误认：字母 O/o↔数字 0、l/I↔1
_OCR_CONFUSE = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1"})


def file_roots():
    """所有已登录账号的 msg/file 目录。"""
    roots = []
    if not os.path.isdir(CONTAINER):
        return roots
    for acc in sorted(os.listdir(CONTAINER)):
        d = os.path.join(CONTAINER, acc, "msg", "file")
        if os.path.isdir(d):
            roots.append(d)
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
        self.entries = []          # [{path, name, norm, ocrnorm, mtime, size}]
        self.by_norm = {}
        for root in (roots if roots is not None else file_roots()):
            for dirpath, _dirs, files in os.walk(root):
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


def resolve_and_collect(msgs, dest_dir=None, max_copy_mb=200, progress=None):
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
        e, conf = idx.find(m.get("fname", ""))
        if not e:
            m["fnote"] = "本机没有这个文件(没下载过或已过期)"
            continue
        hit += 1
        m["fpath"] = e["path"]
        m["fsize_real"] = human_size(e["size"])
        m["fconf"] = conf
        if dest_dir:
            if e["size"] > max_copy_mb * 1024 * 1024:
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
    say(f"· 文件匹配完成：{hit}/{len(files)} 个找到了真实文件")
    return hit, len(files)
