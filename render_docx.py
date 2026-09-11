#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把解析出的消息渲染成仿微信排版的 .docx。"""
import os
from urllib.parse import quote
from docx import Document
from docx.shared import Pt, RGBColor, Inches, Emu
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

GREEN = "A9EA7A"      # 我方气泡
GRAY = "3A3A3C"       # 对方气泡(浅色文档里用浅灰更合适)
GRAY_LIGHT = "ECECEC"
ME_LABEL = "我"


def _shade(cell, color):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear'); shd.set(qn('w:color'), 'auto'); shd.set(qn('w:fill'), color)
    tcPr.append(shd)


def _no_borders(table):
    tbl = table._tbl
    tblPr = tbl.tblPr
    borders = OxmlElement('w:tblBorders')
    for edge in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'):
        e = OxmlElement(f'w:{edge}')
        e.set(qn('w:val'), 'none'); e.set(qn('w:sz'), '0')
        e.set(qn('w:space'), '0'); e.set(qn('w:color'), 'auto')
        borders.append(e)
    tblPr.append(borders)


def _set_w(cell, inches):
    cell.width = Inches(inches)
    tcPr = cell._tc.get_or_add_tcPr()
    w = OxmlElement('w:tcW')
    w.set(qn('w:w'), str(int(inches * 1440))); w.set(qn('w:type'), 'dxa')
    tcPr.append(w)


def _add_hyperlink(paragraph, url, text, size=8, color=RGBColor(0x1A, 0x5F, 0xB4)):
    """python-docx 没有原生超链接 API，手工塞 w:hyperlink 关系。"""
    part = paragraph.part
    r_id = part.relate_to(url,
                          "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
                          is_external=True)
    link = OxmlElement('w:hyperlink')
    link.set(qn('r:id'), r_id)
    run = OxmlElement('w:r')
    rPr = OxmlElement('w:rPr')
    u = OxmlElement('w:u'); u.set(qn('w:val'), 'single'); rPr.append(u)
    c = OxmlElement('w:color'); c.set(qn('w:val'), str(color)); rPr.append(c)
    sz = OxmlElement('w:sz'); sz.set(qn('w:val'), str(int(size * 2))); rPr.append(sz)
    run.append(rPr)
    t = OxmlElement('w:t')
    t.set(qn('xml:space'), 'preserve')
    t.text = text
    run.append(t)
    link.append(run)
    paragraph._p.append(link)
    return link


def render(im, msgs, title, out_path, media_dir=None, them_name="对方", scale=2.0):
    if media_dir is None:
        media_dir = os.path.splitext(out_path)[0] + "_图片"
    os.makedirs(media_dir, exist_ok=True)

    doc = Document()
    # 页边距
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Inches(0.6)

    h = doc.add_heading(title, level=0)
    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = sub.add_run(f"微信聊天记录导出 · 共 {sum(1 for m in msgs if m['type']!='time')} 条消息")
    r.font.size = Pt(9); r.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
    doc.add_paragraph()

    BUBBLE_W = 3.2   # 气泡最大宽(英寸)
    PAGE_W = 6.8
    n_img = 0
    prev_label = None

    for m in msgs:
        if m["type"] in ("time", "system"):
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(m["text"])
            run.font.size = Pt(8.5)
            run.font.color.rgb = RGBColor(0x99, 0x99, 0x99)
            run.italic = (m["type"] == "system")
            prev_label = None
            continue

        me = (m["sender"] == "me")
        label = ME_LABEL if me else (m.get("name") or them_name)
        table = doc.add_table(rows=1, cols=2)
        table.autofit = False
        _no_borders(table)
        left, right = table.rows[0].cells
        _set_w(left, PAGE_W / 2); _set_w(right, PAGE_W / 2)
        cell = right if me else left

        # 发送方小标签(仅在切换发送方时显示)
        if label != prev_label:
            lp = cell.paragraphs[0]
            lp.alignment = WD_ALIGN_PARAGRAPH.RIGHT if me else WD_ALIGN_PARAGRAPH.LEFT
            nm = lp.add_run(label)
            nm.font.size = Pt(8); nm.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
        else:
            cell.paragraphs[0]._p.getparent().remove(cell.paragraphs[0]._p)
        prev_label = label

        if m["type"] == "text":
            # 嵌套单个单元格做气泡(底纹可靠渲染)
            inner = cell.add_table(rows=1, cols=1)
            inner.alignment = WD_TABLE_ALIGNMENT.RIGHT if me else WD_TABLE_ALIGNMENT.LEFT
            inner.autofit = True
            ic = inner.rows[0].cells[0]
            _shade(ic, GREEN if me else GRAY_LIGHT)
            ip = ic.paragraphs[0]
            ip.alignment = WD_ALIGN_PARAGRAPH.LEFT
            if m.get("voice"):
                suffix = "转文字" if m["text"] else "未转文字"
                vr = ip.add_run(f"🎤 语音{m['dur']} · {suffix}")
                vr.font.size = Pt(8); vr.italic = True
                vr.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
                if m["text"]:
                    ip.add_run().add_break()
            for j, ln in enumerate(m["text"].split("\n") if m["text"] else []):
                if j:
                    ip.add_run().add_break()
                r = ip.add_run(ln)
                r.font.size = Pt(11); r.font.color.rgb = RGBColor(0x11, 0x11, 0x11)
        elif m["type"] == "file":
            # 文件卡片：仿微信的浅色方框，写清文件名/大小/真实位置
            inner = cell.add_table(rows=1, cols=1)
            inner.alignment = WD_TABLE_ALIGNMENT.RIGHT if me else WD_TABLE_ALIGNMENT.LEFT
            inner.autofit = True
            ic = inner.rows[0].cells[0]
            _shade(ic, GREEN if me else GRAY_LIGHT)
            ip = ic.paragraphs[0]
            size = m.get("fsize_real") or m.get("fsize") or ""
            nr = ip.add_run(f"📎 {m['fname']}")
            nr.font.size = Pt(10.5); nr.bold = True
            nr.font.color.rgb = RGBColor(0x11, 0x11, 0x11)
            if size:
                ip.add_run().add_break()
                sr = ip.add_run(size)
                sr.font.size = Pt(8); sr.font.color.rgb = RGBColor(0x77, 0x77, 0x77)
            # 位置只写用户选定的那一个
            chosen = (("位置：", m.get("fcopy")) if m.get("fcopy")
                      else ("位置（微信原始）：", m.get("fpath")))
            for label, path in (chosen,):
                if not path:
                    continue
                ip.add_run().add_break()
                lr = ip.add_run(label)
                lr.font.size = Pt(8); lr.font.color.rgb = RGBColor(0x77, 0x77, 0x77)
                full = path if os.path.isabs(path) else os.path.join(
                    os.path.dirname(os.path.abspath(out_path)), path)
                _add_hyperlink(ip, "file://" + quote(full), path)
            if m.get("fnote"):
                ip.add_run().add_break()
                wr = ip.add_run("⚠️ " + m["fnote"])
                wr.font.size = Pt(8); wr.italic = True
                wr.font.color.rgb = RGBColor(0xB0, 0x50, 0x20)
        else:  # media
            crop = im.crop((m["x0"], m["y0"], m["x1"], m["y1"]))
            n_img += 1
            fp = os.path.join(media_dir, f"img_{n_img:03d}.png")
            crop.save(fp)
            wpx = m["x1"] - m["x0"]
            win_inch = min(BUBBLE_W, max(1.3, wpx / scale / 96.0))
            bp = cell.add_paragraph()
            bp.alignment = WD_ALIGN_PARAGRAPH.RIGHT if me else WD_ALIGN_PARAGRAPH.LEFT
            try:
                bp.add_run().add_picture(fp, width=Inches(win_inch))
            except Exception:
                bp.add_run("[图片]")

        sp = doc.add_paragraph(); sp.paragraph_format.space_after = Pt(2)

    doc.save(out_path)
    return out_path, n_img


if __name__ == "__main__":
    import sys
    from parse3 import parse_image
    stitched = sys.argv[1] if len(sys.argv) > 1 else "stitched.png"
    title = sys.argv[2] if len(sys.argv) > 2 else "红鲤鱼照明&智能灯光定制"
    im, msgs = parse_image(stitched)
    out, n = render(im, msgs, title, "微信导出_测试.docx", them_name="对方")
    print(f"已生成 {out}，嵌入 {n} 张图片，消息 {len(msgs)} 条")
