#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 macOS Vision 引擎对图片做中文OCR，返回文字行 + 像素坐标框。"""
import re, sys, os, json, shutil, subprocess, tempfile
import threading
import Vision
import Quartz
from Foundation import NSURL

# pyobjc 的框架符号是「第一次访问时才去解析」的，而这个解析过程不是线程安全的：
# 分片 OCR 并行跑时两个线程同时首次访问同一个符号，实测抛
# KeyError: 'CGImageSourceCreateImageAtIndex'。在这里(单线程的导入期)先摸一遍，
# 之后多线程访问到的都是已解析好的属性。
_WARM = (Quartz.CGImageSourceCreateWithURL, Quartz.CGImageSourceCreateImageAtIndex,
         Quartz.CGImageGetWidth, Quartz.CGImageGetHeight,
         Vision.VNRecognizeTextRequest, Vision.VNImageRequestHandler,
         Vision.VNRequestTextRecognitionLevelAccurate)

_OCR_LOCK = threading.RLock()
_OCR_MODE = (None, False)
USE_WORKER = False


def _use_cpu(request):
    request.setUsesCPUOnly_(True)
    # 新系统按计算阶段分配设备；旧 usesCPUOnly 属性不足以确认实际设备。
    if not hasattr(request, 'supportedComputeStageDevicesAndReturnError_'):
        return
    try:
        stages, error = request.supportedComputeStageDevicesAndReturnError_(None)
        if error is not None:
            return
        for stage, devices in (stages or {}).items():
            cpu = next((device for device in devices
                        if type(device).__name__ == 'MLCPUComputeDevice'), None)
            if cpu is not None:
                request.setComputeDevice_forComputeStage_(cpu, stage)
    except (AttributeError, TypeError, ValueError):
        pass  # 较早系统保留 usesCPUOnly 路径。


def ocr_image(path):
    if USE_WORKER:
        with _OCR_LOCK:
            return _worker_ocr(path)
    return _ocr_native(path)


def _worker_ocr(path):
    global _OCR_MODE
    fd, temporary = tempfile.mkstemp(prefix='wechat-ocr-', suffix='.png')
    os.close(fd)
    try:
        shutil.copyfile(path, temporary)
        if getattr(sys, 'frozen', False):
            worker = os.path.join(os.path.dirname(sys.executable), 'OCRWorker')
            if not os.path.isfile(worker):
                worker = os.path.join(sys._MEIPASS, 'OCRWorker')
            command = [worker]
        else:
            command = [sys.executable, os.path.join(os.path.dirname(__file__), 'ocr_worker.py')]
        environment = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
        result = subprocess.run(command + [temporary, json.dumps(_OCR_MODE)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=120, env=environment,
                                cwd=tempfile.gettempdir())
        try:
            payload = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise RuntimeError('OCR 后台进程未返回识别结果：' + result.stderr[-300:])
        if result.returncode or not payload.get('ok'):
            raise RuntimeError('OCR 后台识别失败：' + str(payload.get('error', result.stderr[-300:])))
        _OCR_MODE = tuple(payload['mode'])
        return payload['lines'], payload['width'], payload['height']
    except subprocess.TimeoutExpired as error:
        raise RuntimeError('OCR 后台识别超时，当前截图已保留，可重试恢复抓取。') from error
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _ocr_native(path):
    global _OCR_MODE
    url = NSURL.fileURLWithPath_(path)
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    W = Quartz.CGImageGetWidth(cg)
    H = Quartz.CGImageGetHeight(cg)

    # Vision 的加速执行流可能临时失败。串行执行识别请求，失败时用新请求
    # 在 CPU 上重试相同的准确识别模型，不降成快速/低精度识别。
    with _OCR_LOCK:
        detail = "Vision 未完成识别"
        modes = [_OCR_MODE] + [mode for mode in ((None, False), (None, True), (2, True))
                              if mode != _OCR_MODE]
        for revision, cpu_only in modes:
            req = Vision.VNRecognizeTextRequest.alloc().init()
            if revision is not None:
                req.setRevision_(revision)
            req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
            req.setRecognitionLanguages_(["zh-Hans", "en-US"])
            req.setUsesLanguageCorrection_(True)
            if cpu_only:
                _use_cpu(req)
            handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
            try:
                ok, err = handler.performRequests_error_([req], None)
                if ok and err is None:
                    _OCR_MODE = (revision, cpu_only)
                    break
                detail = str(err or detail)
            except Exception as error:
                detail = str(error)
        else:
            raise RuntimeError("OCR 文字识别失败（准确识别、CPU 及兼容模型重试均未完成）：" + detail)
    lines = []
    for obs in (req.results() or []):
        cand = obs.topCandidates_(1)
        if not cand:
            continue
        text = cand[0].string()
        conf = cand[0].confidence()
        b = obs.boundingBox()  # 归一化, 原点左下
        x = b.origin.x * W
        w = b.size.width * W
        h = b.size.height * H
        y = (1 - b.origin.y - b.size.height) * H  # 翻成左上原点
        lines.append({"text": text, "conf": float(conf),
                      "x": x, "y": y, "w": w, "h": h})
    lines.sort(key=lambda l: l["y"])
    return lines, W, H


def ocr_image_sliced(path, slice_h=1800, overlap=240):
    """把高图切成横条分别OCR(保持原分辨率), 合并去重。返回 (lines, W, H)。"""
    from PIL import Image
    import tempfile, os
    im = Image.open(path).convert("RGB")
    W, H = im.size
    if H <= slice_h:
        result = ocr_image(path)
        return result if result[0] else ocr_image(path)
    # 各分片互不依赖，并行跑。Vision 是 ObjC 框架调用，执行期间会放开 GIL，
    # 所以线程池能真正跑满多核；长会话的 OCR 是整个导出里最慢的一步。
    from concurrent.futures import ThreadPoolExecutor
    tmpd = tempfile.mkdtemp()
    offsets = []
    y = 0
    while y < H:
        offsets.append(y)
        if y + slice_h >= H:
            break
        y += slice_h - overlap
    # 最后一片若只是个矮条(整除剩下的余数)，Vision 对它的识别会明显变差：
    # 实测 307px 高的尾片把最后一条消息整条漏掉，而同一块区域放进 1800px 的
    # 正常切片里就能稳定读出来(可复现，不是随机)。所以把尾片改成贴着底边的整片。
    # 但贴底整片会和上一片大面积重叠(实测 1700px)：同一条消息被两片各报一次、
    # y 差几像素就躲过了去重，凭空多出十几条。所以给尾片划一条起始线，
    # 只采纳正常交接点之后的行，重叠部分仍由上一片负责。
    cutoff = {}
    if len(offsets) > 1 and H - offsets[-1] < slice_h * 0.6:
        handoff = offsets[-2] + slice_h - overlap
        offsets[-1] = max(0, H - slice_h)
        cutoff[offsets[-1]] = handoff

    def run_slice(y):
        y1 = min(H, y + slice_h)
        fp = os.path.join(tmpd, f"s_{y}.png")
        im.crop((0, y, W, y1)).save(fp)
        try:
            part, _, _ = ocr_image(fp)
        finally:
            os.remove(fp)
        lo = cutoff.get(y, 0)
        out = []
        for l in part:
            l = dict(l); l["y"] += y
            if l["y"] >= lo:
                out.append(l)
        return out

    workers = min(2, max(1, len(offsets)))
    lines = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(run_slice, y) for y in offsets]
        parts = []
        for future in futures:
            try:
                parts.append(future.result())
            except RuntimeError:
                parts.append(None)
    # Vision 偶尔在并发识别时返回空结果或失败；线程池结束后逐片重试，
    # 不能把识别失败当作“这一片没有文字”交给解析器继续导出。
    for y, part in zip(offsets, parts):
        if not part:
            part = run_slice(y)
        lines.extend(part)
    # 去重(重叠区同一行出现两次)：同一位置的两次识别文本可能略有差异
    # (多个空格/图标符号)，故按"归一化后互相包含 + x范围重叠"判断物理重复
    def _n(s):
        return re.sub(r'[\s•·]+', '', s)
    lines.sort(key=lambda l: (l["y"], l["x"]))
    dedup = []
    for l in lines:
        dup = False
        for k in reversed(dedup):
            if l["y"] - k["y"] > 16:
                break
            a, b = _n(k["text"]), _n(l["text"])
            if a and b and (a in b or b in a):
                lo = max(k["x"], l["x"])
                hi = min(k["x"] + k["w"], l["x"] + l["w"])
                if hi - lo > 0.6 * min(k["w"], l["w"]):
                    dup = True
                    break
        if not dup:
            dedup.append(l)
    try:
        os.rmdir(tmpd)
    except OSError:
        pass
    return dedup, W, H


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "stitch_inspect.png"
    lines, W, H = ocr_image(path)
    print(f"图片 {W}x{H}, 识别 {len(lines)} 行:\n")
    for l in lines:
        side = "右(我)" if (l["x"] + l["w"] / 2) > W * 0.5 else "左"
        print(f"  y={l['y']:5.0f} x={l['x']:5.0f} w={l['w']:4.0f} [{side}] c={l['conf']:.2f}  {l['text']}")
