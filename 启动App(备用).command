#!/bin/zsh
# 备用启动方式：通过终端打开图形界面App（用终端已有的权限，最稳）
cd "$(dirname "$0")"
./.venv/bin/python app.py
