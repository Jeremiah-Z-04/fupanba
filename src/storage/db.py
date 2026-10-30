"""SQLite 主库：连接管理 + 建表 + 版本迁移

表设计见 PRD / MVP1 任务清单：
  daily_zt_pool   每日涨停/炸板池快照（原始数据，永不覆盖历史）
  lianban_state   连板滚动状态（每个 code 一行，每日盘后更新）
  concept_member  静态概念成分缓存（周更，可整表重建）
  theme_result    每日题材结果（只统计簇内涨停/异动家数，不做强度打分）
  leader_result   题材内排序结果（algo_rank + final_rank + override 标记）
  override_log    人工修正日志（调参反馈闭环 + 人工干预次数指标）
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS daily_zt_pool (
    trade_date    TEXT NOT NULL,
    code          TEXT NOT NULL,
    name          TEXT NOT NULL,
    zt_time       TEXT DEFAULT '',
    last_zt_time  TEXT DEFAULT '',
    fengdan_amt   REAL DEFAULT 0,
    amount        REAL DEFAULT 0,
    float_mv      REAL DEFAULT 0,
    turnover      REAL DEFAULT 0,
    zhaban_count  INTEGER DEFAULT 0,
    high_pct      REAL DEFAULT 0,
    fall_pct      REAL DEFAULT 0,
    industry      TEXT DEFAULT '',
    is_st         INTEGER DEFAULT 0,
    is_yizi       INTEGER DEFAULT 0,
    is_bj         INTEGER DEFAULT 0,
    sealed        INTEGER DEFAULT 1,        -- 1=封板涨停 2=10cm异动(非涨停强势) 0=炸板
    reason        TEXT DEFAULT '',          -- 涨停原因/异动原因（同花顺 limit_up_reason 或 keyword_list）
    lianban_height INTEGER DEFAULT 0,       -- 连板高度（当天自维护，历史=continue_day_cnt）
    anomaly_tag   TEXT DEFAULT '',          -- 异动类型（同花顺 tag_name：大涨/快速拉升）
    anomaly_content TEXT DEFAULT '',        -- 异动解读原文（同花顺 analysis_content，仅当日有效）
    source        TEXT DEFAULT '',
    fetched_at    TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (trade_date, code, sealed)
);

CREATE TABLE IF NOT EXISTS lianban_state (
    code          TEXT PRIMARY KEY,
    name          TEXT DEFAULT '',
    height        INTEGER DEFAULT 0,        -- 当前连板高度，断板清零
    last_zt_date  TEXT DEFAULT '',          -- 最近一次涨停日期
    yizi_count    INTEGER DEFAULT 0,        -- 本轮连板中的一字板数量
    updated_at    TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS concept_member (
    concept_name  TEXT NOT NULL,
    code          TEXT NOT NULL,
    source        TEXT DEFAULT '',
    updated_at    TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (concept_name, code)
);

CREATE TABLE IF NOT EXISTS theme_result (
    trade_date    TEXT NOT NULL,
    theme_name    TEXT NOT NULL,
    zt_count      INTEGER DEFAULT 0,        -- 簇内封板涨停家数（页面只显示这个）
    strong_count  INTEGER DEFAULT 0,        -- 簇内创/科异动家数
    member_count  INTEGER DEFAULT 0,        -- 簇内总家数
    cluster_mode  TEXT DEFAULT 'reason',
    created_at    TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (trade_date, theme_name)
);

CREATE TABLE IF NOT EXISTS leader_result (
    trade_date    TEXT NOT NULL,
    theme_name    TEXT NOT NULL,
    code          TEXT NOT NULL,
    name          TEXT DEFAULT '',
    algo_rank     INTEGER DEFAULT 0,        -- 算法排名（永不被人工修改覆盖）
    final_rank    INTEGER DEFAULT 0,        -- 最终排名（人工覆盖优先）
    lianban_height INTEGER DEFAULT 0,
    fengdan_amt   REAL DEFAULT 0,           -- 封单金额（元）
    float_mv      REAL DEFAULT 0,           -- 流通市值（元）
    zt_time       TEXT DEFAULT '',          -- 首次涨停时间
    turnover      REAL DEFAULT 0,           -- 换手率（%）
    high_pct      REAL DEFAULT 0,           -- 当日涨幅（%），创/科异动股排序用
    is_override   INTEGER DEFAULT 0,
    is_removed    INTEGER DEFAULT 0,        -- 软删除：人工移出题材，复原时可恢复
    leader_label  TEXT DEFAULT '',          -- 龙1/龙2/龙3...，非龙标为空
    sub_tags      TEXT DEFAULT '',          -- 命中的二级细分词，逗号分隔（框架层级展示用）
    PRIMARY KEY (trade_date, theme_name, code)
);

CREATE TABLE IF NOT EXISTS override_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date    TEXT NOT NULL,
    theme_name    TEXT NOT NULL,
    code          TEXT NOT NULL,
    action        TEXT NOT NULL,            -- rerank / move_out / move_in
    old_rank      INTEGER,
    new_rank      INTEGER,
    detail        TEXT DEFAULT '',
    created_at    TEXT DEFAULT (datetime('now','localtime'))
);

-- 全市场情绪宽度（采集当日快照算出；历史日期无快照，不写这张表）
CREATE TABLE IF NOT EXISTS market_breadth (
    trade_date  TEXT PRIMARY KEY,
    up_99       INTEGER DEFAULT 0,          -- 涨幅 >=9.90% 家数
    limit_up    INTEGER DEFAULT 0,          -- 涨停家数（题材池口径）
    tech_10     INTEGER DEFAULT 0,          -- 创/科/北 涨幅 >10% 家数
    total       INTEGER DEFAULT 0,          -- 快照总家数（对照用）
    captured_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 题材框架（开盘啦层级表：一级题材 -> 细分词），支持网页端人工增删改。
-- source 区分来源：kpl=框架导入（重导时整批替换）/ manual=网页手工添加（永不覆盖）
CREATE TABLE IF NOT EXISTS kpl_theme (
    theme_name  TEXT NOT NULL,              -- 一级题材，如「体育产业」
    sub_name    TEXT NOT NULL DEFAULT '',   -- 细分词，如「冰雪产业」；空串=题材名本身作锚点
    source      TEXT DEFAULT 'kpl',         -- kpl / manual
    enabled     INTEGER DEFAULT 1,          -- 0=停用（不参与归类，但留档）
    note        TEXT DEFAULT '',
    sort_order  INTEGER DEFAULT 0,
    updated_at  TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (theme_name, sub_name)
);

CREATE INDEX IF NOT EXISTS idx_zt_date ON daily_zt_pool(trade_date);
CREATE INDEX IF NOT EXISTS idx_leader_date ON leader_result(trade_date);
CREATE INDEX IF NOT EXISTS idx_override_date ON override_log(trade_date);
CREATE INDEX IF NOT EXISTS idx_kpl_theme ON kpl_theme(theme_name);
"""


class DB:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：FastAPI 同步端点在线程池中执行，连接跨线程复用；
        # 配合 _lock 保证并发读写安全（本地单用户工具，串行即可）。
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        self._migrate()

    def _migrate(self) -> None:
        cur = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
        )
        if not cur.fetchone():
            self.conn.executescript(DDL)
            self.conn.execute(
                "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)"
            )
            self.conn.execute(
                "INSERT INTO meta VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self.conn.commit()
        else:
            # 已有库：增量迁移（幂等，缺列补列）
            # 先补建新增的表（DDL 全是 IF NOT EXISTS，重复执行安全）——
            # 否则旧库永远拿不到 market_breadth 这类后加的表
            self.conn.executescript(DDL)
            self._ensure_column("daily_zt_pool", "reason", "TEXT DEFAULT ''")
            self._ensure_column("daily_zt_pool", "lianban_height", "INTEGER DEFAULT 0")
            self._ensure_column("daily_zt_pool", "anomaly_tag", "TEXT DEFAULT ''")
            self._ensure_column("daily_zt_pool", "anomaly_content", "TEXT DEFAULT ''")
            self._ensure_column("leader_result", "is_removed", "INTEGER DEFAULT 0")
            self._ensure_column("leader_result", "leader_label", "TEXT DEFAULT ''")
            self._ensure_column("leader_result", "fengdan_amt", "REAL DEFAULT 0")
            self._ensure_column("leader_result", "float_mv", "REAL DEFAULT 0")
            self._ensure_column("leader_result", "zt_time", "TEXT DEFAULT ''")
            self._ensure_column("leader_result", "turnover", "REAL DEFAULT 0")
            self._ensure_column("leader_result", "high_pct", "REAL DEFAULT 0")
            self._ensure_column("leader_result", "sub_tags", "TEXT DEFAULT ''")
            self._ensure_column("theme_result", "zt_count", "INTEGER DEFAULT 0")
            self._ensure_column("theme_result", "strong_count", "INTEGER DEFAULT 0")

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            self.conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def executemany(self, sql: str, seq) -> None:
        with self._lock:
            self.conn.executemany(sql, seq)
            self.conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self.conn.close()
