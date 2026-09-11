#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信聊天记录导出 命令行版(全量自动导出，不带图形选择界面)。
用法: 先在微信打开会话，再运行:
    ./.venv/bin/python wxexport.py --format docx,md
图形界面请用 微信导出.app 或 启动App(备用).command
"""
import argparse, os, time
from engine import capture_and_parse
from render_docx import render
from render_md import render_md
from wxfiles import resolve_and_collect
from paths import make_export_dir, layout


def main():
    ap = argparse.ArgumentParser(description="微信聊天记录导出 CLI")
    ap.add_argument("--name", help="文件名/标题(默认自动识别会话名)")
    ap.add_argument("--max", type=int, default=500, help="最大滚动屏数")
    ap.add_argument("--format", default="docx", help="docx / md / docx,md")
    ap.add_argument("--from-top", action="store_true",
                    help="先自动滚到会话最开头，导出整段聊天记录")
    ap.add_argument("--voice", action="store_true", help="语音自动转文字(实验,较慢)")
    ap.add_argument("--out", help="指定导出文件夹(默认 导出结果/会话名_月日-时分/)")
    ap.add_argument("--keep-image", action="store_true", help="保留拼接长图")
    ap.add_argument("--from-image", help="不抓取，直接重新解析已有的拼接长图(改了解析规则后重出文档用)")
    ap.add_argument("--file-mode", choices=["copy", "link"], default="copy",
                    help="copy=文件复制进导出文件夹(默认)；link=只在文档里写微信原始位置")
    ap.add_argument("--no-copy-files", action="store_true",
                    help="等同 --file-mode link（旧参数，保留兼容）")
    args = ap.parse_args()

    print("== 微信聊天记录导出(命令行) ==")
    if args.from_image:
        # 重解析已有长图：不动微信、不重滚，几秒出结果
        from parse3 import parse_image
        print(f"· 重新解析 {args.from_image} ...")
        im, msgs = parse_image(args.from_image)
        res = {"im": im, "msgs": msgs, "title": args.name or "微信会话",
               "scale": 2.0, "stitched_path": args.from_image}
        _finish(args, res)
        return

    print("即将自动滚动微信，请勿操作鼠标键盘。3秒后开始...")
    for i in (3, 2, 1):
        print(f"  {i}...", end="", flush=True); time.sleep(1)
    print(" 开始!\n")

    try:
        res = capture_and_parse(args.max, args.voice, progress=print,
                                from_top=args.from_top)
    except RuntimeError as e:
        print("!!", e); return

    _finish(args, res)


def _finish(args, res):
    im, msgs, scale = res["im"], res["msgs"], res["scale"]
    title = args.name or res["title"]
    n_msg = sum(1 for m in msgs if m["type"] != "time")
    if args.out:
        d = os.path.abspath(args.out); os.makedirs(d, exist_ok=True)
    else:
        d = make_export_dir(title)
    lay = layout(d)
    fmts = [f.strip().lower() for f in args.format.split(",") if f.strip()]
    date = time.strftime("%Y-%m-%d %H:%M")

    # 聊天里的文件：微信把它们明文存在本地，按文件名找回来，一并放进本次导出文件夹
    link_only = args.no_copy_files or args.file_mode == "link"
    n_hit, n_file = resolve_and_collect(
        msgs, None if link_only else lay["files"], progress=print)

    outputs = []
    if "docx" in fmts:
        o, _ = render(im, msgs, title, lay["docx"], media_dir=lay["images"],
                      scale=scale); outputs.append(o)
    if "md" in fmts or "markdown" in fmts:
        o, _ = render_md(im, msgs, title, lay["md"], media_dir=lay["images"],
                         scale=scale, export_date=date); outputs.append(o)

    print(f"\n✓ 导出完成 ({n_msg} 条消息" + (f"，{n_hit}/{n_file} 个文件已定位" if n_file else "") + ")")
    print("  导出文件夹:", d)
    for o in outputs:
        print("    ·", os.path.basename(o))

    sp = res.get("stitched_path")
    if args.from_image:
        pass
    elif args.keep_image and sp:
        print("  · 拼接长图:", sp)
    elif sp and os.path.exists(sp):
        os.remove(sp)
    if outputs:
        os.system(f'open "{d}"')


if __name__ == "__main__":
    main()
