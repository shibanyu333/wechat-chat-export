"""微信本地目录的访问检查；权限拒绝不能当成没有下载过。"""
import os


DATA_ACCESS_HELP = (
    "无法读取微信本地数据。请到系统设置 → 隐私与安全性 → 文件与文件夹 → "
    "微信聊天记录导出，开启「微信」，然后退出并重新打开导出工具。"
)


def account_dirs(container):
    try:
        names = os.listdir(container)
    except FileNotFoundError:
        return []
    except PermissionError as error:
        raise PermissionError(DATA_ACCESS_HELP) from error
    return [os.path.join(container, name) for name in sorted(names)]


def walk_error(error):
    if isinstance(error, PermissionError):
        raise PermissionError(DATA_ACCESS_HELP) from error
    raise error
