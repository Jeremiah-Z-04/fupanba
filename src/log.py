"""日志系统：控制台 + 按日滚动文件

用法:
    from src.log import get_logger
    log = get_logger(__name__)
    log.info("...")
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_initialized = False


def setup(log_dir: Path, level: int = logging.INFO) -> None:
    global _initialized
    if _initialized:
        return
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)

    file_handler = TimedRotatingFileHandler(
        log_dir / "themehunter.log", when="midnight",
        backupCount=30, encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console)
    root.addHandler(file_handler)
    _initialized = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
