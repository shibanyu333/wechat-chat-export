#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把解析出的消息渲染成便于 AI 读取的 Markdown。"""
import os


def render_md(im, msgs, title, out_path, media_dir=None, them_name="对方", scale=2.0,
              export_date=""):
    if media_dir is None:
        media_dir = os.path.splitext(out_path)[0] + "_图片"
    os.makedirs(media_dir, exist_ok=True)
    media_name = os.path.basename(media_dir)

    n_msg = sum(1 for m in msgs if m["type"] != "time")
    lines = [f"# {title} — 微信聊天记录", ""]
    meta = f"> 共 {n_msg} 条消息"
    if export_date:
        meta += f" · 导出于 {export_date}"
    lines += [meta, "", "---", ""]

    n_img = 0
    for m in msgs:
        if m["type"] == "time":
            lines += ["", f"### 🕐 {m['text']}", ""]
            continue
        if m["type"] == "system":
            lines += [f"*〔{m['text']}〕*", ""]
            continue
        who = "我" if m["sender"] == "me" else (m.get("name") or them_name)
        if m["type"] == "text":
            body = m["text"].replace("\n", "  \n> ")
            if m.get("voice"):
                tag = f"🎤 语音{m['dur']}·转文字" if body else f"🎤 语音{m['dur']}·未转文字"
                lines.append(f"**{who}：** *[{tag}]* {body}".rstrip())
            else:
                lines.append(f"**{who}：** {body}")
        elif m["type"] == "file":
            size = m.get("fsize_real") or m.get("fsize") or ""
            lines.append(f"**{who}：** 📎 **{m['fname']}**" + (f"（{size}）" if size else ""))
            # 位置只写用户选定的那一个：副本(导出文件夹内，相对路径可随文件夹搬走)
            # 或微信原始位置(绝对路径，不复制、不占空间)
            if m.get("fcopy"):
                lines.append(f"> 位置：[`{m['fcopy']}`](<{m['fcopy']}>)")
            elif m.get("fpath"):
                lines.append(f"> 位置（微信原始）：[`{m['fpath']}`](<{m['fpath']}>)")
            if m.get("fnote"):
                lines.append(f"> ⚠️ {m['fnote']}")
        else:  # media
            crop = im.crop((m["x0"], m["y0"], m["x1"], m["y1"]))
            n_img += 1
            fn = f"img_{n_img:03d}.png"
            crop.save(os.path.join(media_dir, fn))
            # 路径用尖括号包起来：文件名里的空格和括号(如"产品分类(2).txt")
            # 直接写进 () 会让 Markdown 提前闭合链接，图片和附件全点不开
            lines.append(f"**{who}：** ![图片](<{media_name}/{fn}>)")
        lines.append("")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out_path, n_img
