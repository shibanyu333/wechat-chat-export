#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出目录规划：每次导出独占一个文件夹，文档 / 图片 / 文件全在里面。

    导出结果/
      客户张三_0903-2100/
        聊天记录.docx
        聊天记录.md
        图片/img_001.png ...
        文件/首页修改.psd ...

整个文件夹可以直接压缩发给别人或归档，不会跟别的会话混在一起；
文件夹名带时间戳，重复导出同一个会话也不会覆盖上一次的结果。
"""
import os, re, time
from appdirs_local import data_root

BASE_DIR = data_root()
OUT_ROOT = os.path.join(BASE_DIR, "导出结果")

DOC_NAME = "聊天记录"          # 文档统一叫这个，会话名体现在文件夹上
IMG_DIR = "图片"
FILE_DIR = "文件"


def safe_name(title, fallback="微信会话"):
    """会话名转成能当文件夹名的字符串。"""
    s = re.sub(r'[/\\:*?"<>|\r\n\t]', '', title or '')
    s = re.sub(r'\s+', ' ', s).strip(' .')
    return s[:40] or fallback


def make_export_dir(title, root=None, stamp=None):
    """建一个本次导出专用的文件夹并返回路径。同名已存在就往后加序号。"""
    root = root or OUT_ROOT
    name = safe_name(title)
    stamp = stamp or time.strftime("%m%d-%H%M")
    d = os.path.join(root, f"{name}_{stamp}")
    n = 2
    while os.path.exists(d):
        d = os.path.join(root, f"{name}_{stamp}-{n}")
        n += 1
    os.makedirs(d, exist_ok=True)
    return d


def layout(export_dir):
    """返回这次导出里各部分的落地路径。"""
    return {
        "dir": export_dir,
        "docx": os.path.join(export_dir, DOC_NAME + ".docx"),
        "md": os.path.join(export_dir, DOC_NAME + ".md"),
        "images": os.path.join(export_dir, IMG_DIR),
        "files": os.path.join(export_dir, FILE_DIR),
    }
