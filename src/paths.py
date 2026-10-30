"""运行目录解析：源码运行 vs PyInstaller 打包运行。

- APP_DIR     可写目录。源码运行 = 项目根；打包运行 = exe 所在目录
              （data/、config/、.env 都放这里，重装/换机只要拷文件夹）
- BUNDLE_DIR  只读资源目录。源码运行 = 项目根；打包运行 = _MEIPASS
              （web 静态文件、默认配置随包发布）

打包后 __file__ 指向临时解压目录（_MEIPASS），不能再用它推项目根，
否则 data/ 会写进临时目录、退出即丢。
"""
from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _source_root() -> Path:
    return Path(__file__).resolve().parent.parent


APP_DIR: Path = Path(sys.executable).resolve().parent if is_frozen() else _source_root()
BUNDLE_DIR: Path = Path(getattr(sys, "_MEIPASS", APP_DIR)) if is_frozen() else APP_DIR
