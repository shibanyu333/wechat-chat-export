"""独立控制台识别进程：不初始化 pywebview/AppKit，不访问微信窗口。"""
import json
import sys
import ocr


def main():
    try:
        if len(sys.argv) != 3:
            raise ValueError('需要图像路径与识别模式')
        ocr._OCR_MODE = tuple(json.loads(sys.argv[2]))
        lines, width, height = ocr._ocr_native(sys.argv[1])
        print(json.dumps({'ok': True, 'lines': lines, 'width': width, 'height': height,
                          'mode': ocr._OCR_MODE}, ensure_ascii=False), flush=True)
        return 0
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False), flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
