#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行时目录：区分「源码运行」和「打包成 .app 后运行」。

打包成独立 App 装到 /Applications 之后，**.app 内部是不可写的**（也不该写：
系统更新或重装会连同里面的东西一起替换掉）。所以导出结果、临时截图这些
必须落到用户目录下。源码运行时仍然放在项目目录里，行为和以前一致。
"""
import os, sys

FROZEN = getattr(sys, "frozen", False)
# 打包后 __file__ 在 .app 内部，不能用来定位可写目录
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "微信聊天记录导出"


def data_root():
    """可写的数据根目录。"""
    d = os.path.expanduser(f"~/Documents/{APP_NAME}") if FROZEN else SRC_DIR
    os.makedirs(d, exist_ok=True)
    return d


def resource_path(*parts):
    """随包分发的只读资源（如 ui/index.html）。"""
    base = getattr(sys, "_MEIPASS", SRC_DIR)
    return os.path.join(base, *parts)
