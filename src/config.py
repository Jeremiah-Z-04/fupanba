"""配置加载：default.yaml + 点路径访问

用法:
    cfg = Config.load()
    cfg.get("clustering.mode")          # -> "reason"
    cfg.get("leader.top_n", 3)          # 带默认值
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.paths import APP_DIR, BUNDLE_DIR

PROJECT_ROOT = APP_DIR
DEFAULT_CONFIG = APP_DIR / "config" / "default.yaml"
if not DEFAULT_CONFIG.exists():
    # 打包运行且用户没放 config/ 时，用包内默认配置（只读，data 仍写 APP_DIR）
    DEFAULT_CONFIG = BUNDLE_DIR / "config" / "default.yaml"


class Config:
    def __init__(self, data: dict, root: Path = PROJECT_ROOT):
        self._data = data
        self.root = root

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        cfg_path = path or DEFAULT_CONFIG
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        # 显式传 path（测试/临时目录）时跟着配置文件走；默认配置始终落到可写 APP_DIR
        root = cfg_path.parent.parent if path else APP_DIR
        return cls(data or {}, root)

    # ---------- 访问 ----------
    def get(self, dot_path: str, default: Any = None) -> Any:
        node: Any = self._data
        for key in dot_path.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def set(self, dot_path: str, value: Any) -> None:
        keys = dot_path.split(".")
        node = self._data
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = value

    def section(self, dot_path: str) -> dict:
        sec = self.get(dot_path, {})
        return sec if isinstance(sec, dict) else {}

    # ---------- 路径 ----------
    @property
    def data_dir(self) -> Path:
        d = self.root / self.get("app.data_dir", "data")
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def db_path(self) -> Path:
        return self.data_dir / "themehunter.db"
