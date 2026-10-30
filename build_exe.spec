# -*- mode: python ; coding: utf-8 -*-
"""复盘吧 打包配置（PyInstaller）

产物：dist/复盘吧/ 文件夹
  ├─ 复盘吧.exe         双击启动（控制台窗口 + 自动开浏览器）
  ├─ _internal/         运行时依赖、web 静态文件、默认配置
  ├─ config/            用户可改配置（启动时优先读这里）
  ├─ data/              数据库/导出/日志（可整体拷贝迁移）
  └─ .env               同花顺 API Key

不打包 akshare/pandas：体积小、构建稳；缺的流通市值/换手率只影响「封单占比」列。
"""

import os
APP_NAME = os.environ.get("TH_APP_NAME", "复盘吧")

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "anyio._backends._asyncio",
]

excludes = [
    "akshare", "pandas", "numpy", "matplotlib", "scipy", "PIL", "tkinter",
    "pytest", "IPython", "pyarrow", "numba", "lxml", "bs4", "requests",
    "notebook", "jupyter", "sqlalchemy", "torch", "transformers",
]

a = Analysis(
    ["start.py"],
    pathex=[],
    binaries=[],
    datas=[("web", "web"), ("config", "config")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
