# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置：把工具打包成不依赖 Python 环境的独立 .app。

要点：
· pyobjc 的框架绑定是运行时动态查找的，PyInstaller 扫不出来，必须显式声明；
· pywebview 的 macOS 后端在 webview.platforms.cocoa，同样要显式带上；
· ui/index.html 是界面本体，必须作为数据文件打进去；
· 只用到 scipy.ndimage，其余子包全部排除，否则包体会白白大出一百多兆；
· 第三方许可证每次打包时从 .venv 现抽，随 App 一起分发。
"""
import os
import sys
import importlib.metadata as md

hidden = [
    # pyobjc：读屏、OCR、窗口枚举、前台化
    'Quartz', 'Vision', 'AppKit', 'Foundation', 'objc',
    'ApplicationServices', 'CoreFoundation',
    # ApplicationServices 是 engine.preflight() 用来查「辅助功能」权限的，
    # 它在函数体里 import，PyInstaller 静态扫不到；漏掉的话打包版一点「开始抓取」
    # 就静默无反应(JS 那边 await 直接 reject，界面毫无提示)。实测踩过。
    # 图形界面
    'webview', 'webview.platforms', 'webview.platforms.cocoa',
    # 计算
    'scipy.ndimage', 'numpy', 'PIL', 'docx',
]

# scipy 的内部依赖是连锁的：ndimage → _lib → linalg → sparse → csgraph
# → sparse.linalg，挑着排会在运行时炸 ModuleNotFoundError(实测踩过两轮)。
# 所以 scipy 整包带上，只排真正没用到的大件。
excludes = ['matplotlib', 'tkinter', 'PyInstaller']


# 安装包里带了 numpy/scipy/Pillow/pywebview/python-docx/pyobjc 和 Python 运行时本身，
# 它们的许可证(BSD/MIT/PSF)都要求分发二进制时附上版权声明。每次打包从 .venv 现抽，
# 依赖升级也不会漏；numpy/scipy/Pillow 自己的 LICENSE 里已经含了它们内嵌的
# OpenBLAS、libjpeg 等动态库的声明。pip/setuptools 只是装包工具，不进 App。
def third_party_notices(out):
    parts = []
    py_lic = os.path.join(sys.base_prefix, 'lib',
                          'python%d.%d' % sys.version_info[:2], 'LICENSE.txt')
    if os.path.exists(py_lic):
        with open(py_lic, encoding='utf-8') as fp:
            parts.append(('Python ' + sys.version.split()[0], 'PSF-2.0', [fp.read()]))
    for d in sorted(md.distributions(), key=lambda d: d.metadata['Name'].lower()):
        name = d.metadata['Name']
        if name.lower() in ('pip', 'setuptools'):
            continue
        texts = [f.read_text(encoding='utf-8') for f in (d.files or [])
                 if '.dist-info/' in str(f)
                 and any(k in f.name.upper() for k in ('LICEN', 'COPYING', 'NOTICE'))]
        lic = (d.metadata.get('License-Expression')
               or (d.metadata.get('License') or '').split('\n')[0])
        parts.append(('%s %s' % (name, d.version), lic, texts))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w', encoding='utf-8') as fp:
        fp.write('本 App 打包了以下第三方软件，各自按其许可证分发。\n\n')
        for title, lic, texts in parts:
            fp.write('=' * 72 + '\n%s  (%s)\n' % (title, lic or '见正文') + '=' * 72 + '\n')
            fp.write('\n\n'.join(texts) if texts else '(该包未随附许可证全文，许可证类型见标题)\n')
            fp.write('\n\n')
    return out


NOTICES = third_party_notices(os.path.join(workpath, 'THIRD_PARTY_LICENSES.txt'))

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('ui/index.html', 'ui'),
           (NOTICES, '.'),
           ('.venv/lib/python3.9/site-packages/ApplicationServices', 'ApplicationServices')],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='WeChatExport',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 图形界面，不要终端窗口
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False, name='WeChatExport',
)
app = BUNDLE(
    coll,
    name='微信聊天记录导出.app',
    icon='icon.icns',
    bundle_identifier='com.shibanyu333.wechatchatexport',
    info_plist={
        'CFBundleName': '微信聊天记录导出',
        'CFBundleDisplayName': '微信聊天记录导出',
        'CFBundleShortVersionString': '1.2.0',
        'CFBundleVersion': '1.2.0',
        'NSHighResolutionCapable': True,
        # 抓取需要「屏幕录制」与「辅助功能」，两者都是系统开关(TCC)，
        # 没有对应的用途描述键；这里写清楚用途，便于用户理解授权弹窗。
        'NSAppleEventsUsageDescription': '用于把微信切到前台并自动滚动聊天窗口。',
        'LSMinimumSystemVersion': '11.0',
        'LSApplicationCategoryType': 'public.app-category.productivity',
    },
)
