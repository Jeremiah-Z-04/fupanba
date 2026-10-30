"""CSV 复盘报表导出"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from src.config import Config
from src.log import get_logger

log = get_logger(__name__)


def export_csv(rows: list[dict], trade_date: date, cfg: Config) -> Path:
    """rows: [{theme_name, score, code, name, leader_rank, lianban_height, ...}]"""
    out_dir = cfg.data_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"theme_leaders_{trade_date.isoformat()}.csv"

    fields = cfg.get("export.csv_fields", [
        "date", "theme_name", "code", "name", "leader_rank", "lianban_height",
    ])
    header_map = {
        "date": "日期", "theme_name": "题材名称",
        "code": "股票代码", "name": "股票名称", "leader_rank": "龙序号",
        "lianban_height": "连板高度",
    }
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([header_map.get(k, k) for k in fields])
        for r in rows:
            writer.writerow([
                trade_date.isoformat() if k == "date" else r.get(k, "")
                for k in fields
            ])
    log.info("CSV 报表已导出: %s（%d 行）", path, len(rows))
    return path
