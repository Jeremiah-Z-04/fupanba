# -*- coding: utf-8 -*-
"""爆量题材：当日成交量 >= 前 N 个交易日「最低那天」成交量的 M 倍。

与涨停题材平行的第二条主线，用于发现「没涨停但在放量」的预备队。

核心口径（用户拍板）：
  - 基准 = 前 base_window(5) 个交易日中**最低**那天的成交量
  - 指标 = 成交量（股），不是成交额
  - 范围 = 全市场
  - 倍数 = 2 / 3 可切换

数据来源：
  - 当日：all_a_share_snapshot() 的 volume（Pipeline.fetch 顺手落库，0 额外请求）
  - 历史：个股日 K 回补（deploy → main.py volfill），一次请求覆盖整段区间
"""
from __future__ import annotations

import datetime
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from src.core.clustering.reason_clusterer import ReasonClusterer
from src.datasources import fuyao_api as fy
from src.log import get_logger
from src.storage.snapshot_repo import SnapshotRepo
from src.storage.volume_repo import VolumeRepo

log = get_logger(__name__)

_SCAN_LOCK = threading.Lock()


# ------------------------------------------------------------------ 配置

def vcfg(cfg) -> dict:
    """把 volume.* 读成一个 dict，调用处不必反复点路径。"""
    return {
        "base_window": int(cfg.get("volume.base_window", 5)),
        "store_min_ratio": float(cfg.get("volume.store_min_ratio", 1.5)),
        "require_full_window": bool(cfg.get("volume.require_full_window", True)),
        "max_themes_per_stock": int(cfg.get("volume.max_themes_per_stock", 3)),
        "reason_lookback_days": int(cfg.get("volume.reason_lookback_days", 5)),
        "min_total": int(cfg.get("volume.min_total", 4000)),
        "backfill_days": int(cfg.get("volume.backfill_days", 20)),
        "backfill_workers": int(cfg.get("volume.backfill_workers", 8)),
        "backfill_qps": float(cfg.get("volume.backfill_qps", 12)),
    }


# ------------------------------------------------------------------ 交易日窗口

def prev_trading_days(cal, trade_date: date, n: int = 5) -> list[str]:
    """严格早于 trade_date 的最近 n 个交易日（升序）。

    必须走交易日历：国庆/春节期间 date-7 会跨掉 4 个交易日，
    基准窗口直接错位，倍数全废。
    """
    if n <= 0:
        return []
    days = [d for d in cal.days() if d < trade_date]
    return [d.isoformat() for d in days[-n:]]


# ------------------------------------------------------------------ 扫描 SQL

_SCAN_SQL = """
WITH w AS (
    SELECT code, trade_date, volume,
           ROW_NUMBER() OVER (PARTITION BY code ORDER BY volume ASC, trade_date ASC) AS rn,
           COUNT(*)    OVER (PARTITION BY code) AS bars,
           AVG(volume) OVER (PARTITION BY code) AS avg_vol
    FROM daily_volume
    WHERE trade_date IN ({ph}) AND volume > 0
)
SELECT t.code, t.volume, t.close, t.prev_close, t.is_snapshot,
       w.volume      AS base_volume,
       w.trade_date  AS base_date,
       w.bars        AS bars,
       w.avg_vol     AS avg_vol,
       t.volume * 1.0 / w.volume AS ratio
FROM daily_volume t
JOIN w ON w.code = t.code AND w.rn = 1
WHERE t.trade_date = ?
  AND t.volume > 0
  AND w.volume > 0
  AND t.volume >= w.volume * ?
  AND w.bars >= ?
ORDER BY ratio DESC
"""


def scan_rows(db, ds: str, window: list[str], min_ratio: float,
              base_window: int, require_full: bool) -> list[dict]:
    """一条 SQL 算出「今日量 / 前N日最低量 / 倍数 / 最低那天」。"""
    if not window:
        return []
    ph = ",".join("?" * len(window))
    need_bars = base_window if require_full else 1
    sql = _SCAN_SQL.format(ph=ph)
    params = (*window, ds, float(min_ratio), int(need_bars))
    return [dict(r) for r in db.query(sql, params)]


# ------------------------------------------------------------------ 名称 / 归因素材

def _name_map(db, codes: list[str]) -> dict[str, str]:
    """code -> 名称。优先全市场名称表，失败回退池内名称。

    名称必须落到 volume_surge.name：静态导出只读库，不能再联网取名。
    """
    out: dict[str, str] = {}
    try:
        names = fy.all_a_share_names() or {}
        for k, v in names.items():
            if k and v:
                out[str(k).split(".")[0]] = str(v)
    except Exception as e:  # noqa: BLE001
        log.warning("取全市场名称失败: %s", e)
    miss = [c for c in codes if c not in out]
    for i in range(0, len(miss), 400):
        batch = miss[i:i + 400]
        ph = ",".join("?" * len(batch))
        for r in db.query(
                f"SELECT code, name FROM daily_zt_pool WHERE code IN ({ph}) "
                f"GROUP BY code", tuple(batch)):
            out.setdefault(r["code"], r["name"])
    return out


def _today_themes(db, ds: str, codes: list[str]) -> dict[str, list[tuple[str, str]]]:
    """当日已归纳结果（涨停股）→ 直接复用，保证两个视图归属一致。"""
    out: dict[str, list[tuple[str, str]]] = {}
    for i in range(0, len(codes), 400):
        batch = codes[i:i + 400]
        ph = ",".join("?" * len(batch))
        for r in db.query(
                f"SELECT code, theme_name, sub_tags FROM leader_result "
                f"WHERE trade_date=? AND is_removed=0 AND code IN ({ph}) "
                f"ORDER BY final_rank", (ds, *batch)):
            out.setdefault(r["code"], []).append(
                (r["theme_name"], r["sub_tags"] or ""))
    return out


def _recent_reasons(db, codes: list[str], ds: str,
                    since: str) -> dict[str, tuple[str, str]]:
    """code -> (reason, reason_date)：时效内最近一次带原因的涨停记录。

    爆量股大多没涨停，reason 靠回看历史；限 since 之后，
    避免拿半年前的老逻辑给今天的爆量贴标签。
    """
    out: dict[str, tuple[str, str]] = {}
    for i in range(0, len(codes), 400):
        batch = codes[i:i + 400]
        ph = ",".join("?" * len(batch))
        for r in db.query(
                f"SELECT code, reason, trade_date FROM daily_zt_pool "
                f"WHERE code IN ({ph}) AND reason <> '' "
                f"AND trade_date <= ? AND trade_date >= ? "
                f"ORDER BY trade_date DESC", (*batch, ds, since)):
            if r["code"] not in out:
                out[r["code"]] = (r["reason"], r["trade_date"])
    return out


# ------------------------------------------------------------------ 归类

def map_codes_to_themes(codes: list[str], ds: str, since: str,
                        clusterer: ReasonClusterer, repo: SnapshotRepo,
                        max_per_stock: int = 3) -> dict[str, dict]:
    """codes -> {code: {themes:[(板块,细分)], reason, reason_date, src}}

    四级优先级：
      1 当日已归纳（涨停股）→ 直接复用 leader_result，两视图口径天然一致
      2 时效内涨停原因 → _extract + _canonicals_detail
      3 同花顺概念 → _is_broad 拦泛概念 → _concept_subject（只认框架内细分词）
      4 都没命中 → 不出现，前端计入「未归类」

    只保留框架内板块：爆量排行按家数排名，让「超跌反弹」这类孤词成板块
    会立刻把排行灌满噪音（这点和 cluster() 的策略相反）。
    """
    if not codes:
        return {}
    framework = set(clusterer._kpl_themes) | {f for _, f in clusterer._patterns}
    sizes = clusterer._concept_sizes()
    zt_map = _today_themes(repo.db, ds, codes)
    reasons = _recent_reasons(repo.db, codes, ds, since)

    out: dict[str, dict] = {}
    for code in codes:
        hits: list[tuple[str, str]] = []
        reason, rdate, src = "", "", ""

        # 1) 当日已归纳
        for theme, sub in zt_map.get(code, []):
            if theme in framework and len(hits) < max_per_stock:
                hits.append((theme, _first_sub(sub)))
        if hits:
            src = "zt_today"

        # 2) 近期涨停原因
        if not hits:
            reason, rdate = reasons.get(code, ("", ""))
            for kw in clusterer._extract(reason):
                for theme, sub in clusterer._canonicals_detail(kw):
                    if theme not in framework:
                        continue
                    if (theme, sub) not in hits:
                        hits.append((theme, sub))
                    if len(hits) >= max_per_stock:
                        break
                if len(hits) >= max_per_stock:
                    break
            if hits:
                src = "reason"

        # 3) 同花顺概念兜底
        if not hits:
            try:
                concepts = repo.concepts_of(code)
            except Exception:  # noqa: BLE001
                concepts = []
            for cpt in concepts:
                if clusterer._is_broad(cpt, sizes):
                    continue
                hit = clusterer._concept_subject(cpt)
                if not hit or hit[0] not in framework:
                    continue
                if hit not in hits:
                    hits.append(hit)
                if len(hits) >= max_per_stock:
                    break
            if hits:
                src = "concept"

        out[code] = {"themes": hits[:max_per_stock], "reason": reason,
                     "reason_date": rdate, "src": src}
    return out


def _first_sub(sub_tags: str) -> str:
    """leader_result.sub_tags 是逗号串，取第一个做二级标签。"""
    if not sub_tags:
        return ""
    return str(sub_tags).split(",")[0].strip()


# ------------------------------------------------------------------ 主流程

def scan_day(cfg, db, cal, trade_date: date, force: bool = False) -> dict:
    """用库里的 daily_volume 算当日爆量 + 归类落库。纯本地，不联网（名称表除外）。"""
    ds = trade_date.isoformat()
    c = vcfg(cfg)
    base_n = c["base_window"]
    window = prev_trading_days(cal, trade_date, base_n)
    if len(window) < base_n:
        return {"ok": False, "reason": "交易日历不足 %d 天，无法算基准" % base_n}

    repo = VolumeRepo(db)
    cov = repo.coverage(window + [ds])
    lack = [d for d in window + [ds] if cov.get(d, 0) < c["min_total"]]
    if lack:
        return {"ok": False, "ready": False,
                "reason": "缺少 %s 的全市场成交量，请先点「爆量回补」" % "、".join(lack)}

    with _SCAN_LOCK:
        rows = scan_rows(db, ds, window, c["store_min_ratio"],
                         base_n, c["require_full_window"])
        if not rows:
            repo.replace_surge(ds, [])
            repo.replace_theme_map(ds, [])
            log.info("[%s] 爆量扫描: 0 只命中", ds)
            return {"ok": True, "surge": 0, "show": 0, "themes": 0}

        codes = [r["code"] for r in rows]
        names = _name_map(db, codes)

        # 只对要展示的 ≥2 倍股归类，省掉一半无用的概念查询
        mults = [int(m) for m in (cfg.get("volume.display_mults", [2, 3]) or [2, 3])]
        show_mult = min(mults) if mults else 2
        show = [r["code"] for r in rows if (r["ratio"] or 0) >= show_mult]

        clusterer = ReasonClusterer(cfg, SnapshotRepo(db))
        look = prev_trading_days(cal, trade_date, c["reason_lookback_days"])
        since = look[0] if look else window[0]
        mapped = map_codes_to_themes(show, ds, since, clusterer,
                                    SnapshotRepo(db), c["max_themes_per_stock"])

        surge_rows, theme_rows = [], []
        for r in rows:
            code = r["code"]
            pc = float(r["prev_close"] or 0)
            close = float(r["close"] or 0)
            pct = round((close / pc - 1) * 100, 2) if pc > 0 and close > 0 else 0.0
            m = mapped.get(code, {})
            surge_rows.append((
                code, names.get(code, code), float(r["volume"] or 0),
                float(r["base_volume"] or 0), r["base_date"] or "",
                float(r["avg_vol"] or 0), int(r["bars"] or 0),
                round(float(r["ratio"] or 0), 3), pct,
                0 if int(r["bars"] or 0) >= base_n else 1,
                (m.get("reason") or "")[:200], m.get("reason_date") or "",
                m.get("src") or "",
            ))
            for theme, sub in (m.get("themes") or []):
                theme_rows.append((theme, code, sub))

        repo.replace_surge(ds, surge_rows)
        repo.replace_theme_map(ds, theme_rows)

    n_theme = len({t for t, _, _ in theme_rows})
    log.info("[%s] 爆量扫描: %d 只命中（≥%d倍 %d 只），归入 %d 个板块 / %d 条归属",
             ds, len(rows), show_mult, len(show), n_theme, len(theme_rows))
    return {"ok": True, "surge": len(rows), "show": len(show), "themes": n_theme}


# ------------------------------------------------------------------ 聚合

def build_payload(db, trade_date: str, cfg=None) -> dict:
    """当日爆量完整 payload。

    API 与静态导出共用这一个函数，防止两处口径漂移。
    一次返回 2/3 两套预聚合 + 原始行：前端切档只改一个变量，零请求，
    且左栏家数与右栏明细必然同源一致。
    """
    repo = VolumeRepo(db)
    mults = [2, 3]
    if cfg is not None:
        mults = [int(m) for m in (cfg.get("volume.display_mults", [2, 3]) or [2, 3])]
    mults = sorted(set(mults)) or [2]
    base_mult = mults[0]

    if not repo.has_surge(trade_date):
        return {"date": trade_date, "ready": False, "partial": False,
                "hint": "该日还没有爆量数据", "base_desc": _base_desc(cfg),
                "base_window": [], "mults": mults, "totals": {},
                "themes": [], "stocks": [], "updated_at": ""}

    surge = repo.load_surge(trade_date, base_mult)          # 已排除 suspect
    members = repo.load_members(trade_date, base_mult)

    # 当日涨停股（板块内「其中涨停 N 只」的交叉依据）
    zt_codes = {r["code"] for r in db.query(
        "SELECT code FROM daily_zt_pool WHERE trade_date=? AND sealed=1",
        (trade_date,))}

    themes: dict[str, dict] = {}
    stocks: list[dict] = []
    mapped_codes: set[str] = set()

    for m in members:
        ratio = float(m["ratio"] or 0)
        if ratio < mults[0]:
            continue
        t = themes.setdefault(m["theme_name"], {
            "theme_name": m["theme_name"],
            "zt_2": 0,
        })
        for mult in mults:
            if ratio >= mult:
                t["count_%d" % mult] = t.get("count_%d" % mult, 0) + 1
                t["max_%d" % mult] = max(t.get("max_%d" % mult, 0.0), ratio)
            t.setdefault("count_%d" % mult, 0)
            t.setdefault("max_%d" % mult, 0.0)
        if m["code"] in zt_codes:
            t["zt_2"] += 1
        mapped_codes.add(m["code"])
        stocks.append({
            "theme": m["theme_name"], "code": m["code"], "name": m["name"],
            "ratio": round(ratio, 2),
            "volume": float(m["volume"] or 0),
            "base_volume": float(m["base_volume"] or 0),
            "base_date": m["base_date"] or "",
            "high_pct": float(m["high_pct"] or 0),
            "suspect": bool(m["is_suspect"]),
            "src": m["map_source"] or "",
            "sub": m["sub_tags"] or "",
            "reason": (m["reason"] or "")[:60],
            "reason_date": m["reason_date"] or "",
            "is_zt": m["code"] in zt_codes,
        })

    th_list = sorted(themes.values(),
                     key=lambda x: (-x.get("count_%d" % mults[0], 0),
                                    -x.get("max_%d" % mults[0], 0.0)))

    surge_codes = {s["code"] for s in surge}
    totals = {}
    for mult in mults:
        sel = [s for s in surge if float(s["ratio"] or 0) >= mult]
        totals["surge_%d" % mult] = len(sel)
        totals["unmapped_%d" % mult] = sum(1 for s in sel if s["code"] not in mapped_codes)
        totals["theme_count_%d" % mult] = sum(
            1 for t in th_list if t.get("count_%d" % mult, 0) > 0)
    totals["suspect"] = int(db.query(
        "SELECT COUNT(*) AS c FROM volume_surge WHERE trade_date=? AND is_suspect=1",
        (trade_date,))[0]["c"])
    totals["unmapped_total"] = len(surge_codes - mapped_codes)

    snap = db.query(
        "SELECT COUNT(*) AS c FROM daily_volume WHERE trade_date=? AND is_snapshot=1",
        (trade_date,))
    partial = bool(snap and snap[0]["c"] > 0)

    row = db.query("SELECT MAX(updated_at) AS u FROM volume_surge WHERE trade_date=?",
                   (trade_date,))
    return {
        "date": trade_date, "ready": True, "partial": partial,
        "hint": "当日为盘中快照，成交量不完整，收盘后请重新采集" if partial else "",
        "base_desc": _base_desc(cfg), "base_window": [],
        "mults": mults, "totals": totals,
        "themes": th_list, "stocks": stocks,
        "updated_at": (row[0]["u"] if row and row[0]["u"] else ""),
    }


def _base_desc(cfg) -> str:
    n = 5
    if cfg is not None:
        try:
            n = int(cfg.get("volume.base_window", 5))
        except Exception:  # noqa: BLE001
            n = 5
    return "基准 = 前 %d 个交易日中的最低成交量" % n


# ------------------------------------------------------------------ 回补

class _RateLimiter:
    """跨线程令牌桶：保证全局 QPS 不超过设定值。"""

    def __init__(self, qps: float):
        self.interval = 1.0 / max(0.5, float(qps))
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep_for = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.interval
        if sleep_for:
            time.sleep(sleep_for)


def _fetch_one(code: str, start_ms: int, end_ms: int, limiter: _RateLimiter,
               retries: int = 3):
    """单只日 K，带 429/4001 指数退避。返回 (code, bars|None)。"""
    ths = fy.to_thscode(code)
    for attempt in range(retries + 1):
        limiter.wait()
        bars = fy.stock_daily(ths, start_ms, end_ms, ttl_group=None)
        if bars is not None:            # [] 也算成功（区间无K线：停牌/新股）
            return code, bars
        err = dict(fy.last_error or {})
        if err.get("code") in (429, 4001):
            time.sleep(min(8.0, 1.2 * (2 ** attempt)) + random.random() * 0.5)
            continue
        time.sleep(0.3 * (attempt + 1))
    return code, None


def _all_codes(db) -> list[str]:
    """全市场代码：优先快照，失败用库内并集兜底。"""
    try:
        snap = fy.all_a_share_snapshot() or {}
        codes = sorted({str(k).split(".")[0] for k in snap})
        if len(codes) >= 4000:
            return codes
    except Exception as e:  # noqa: BLE001
        log.warning("取全市场快照失败，回退库内代码: %s", e)
    rows = db.query(
        "SELECT DISTINCT code FROM daily_volume "
        "UNION SELECT DISTINCT code FROM daily_zt_pool")
    return sorted({r["code"] for r in rows if r["code"]})


def backfill_history(db, cal, cfg, days: int | None = None,
                     on_progress=None, force: bool = False) -> dict:
    """回补全市场逐日成交量。

    一次请求返回该股整段区间，所以 20 天和 6 天成本相同。
    写库在主线程做（worker 只发请求），避免连接竞争。
    """
    c = vcfg(cfg)
    days = int(days or c["backfill_days"])
    today = date.today()
    cal_days = [d for d in cal.days() if d < today]
    if len(cal_days) < days + c["base_window"]:
        days = max(1, len(cal_days) - c["base_window"])

    want = [d.isoformat() for d in cal_days[-days:]]
    window_start = cal_days[-days - c["base_window"]]
    start_ms = fy.sh_date_ms(window_start.isoformat())
    end_ms = fy.sh_date_ms(cal_days[-1].isoformat()) + 86_399_999

    repo = VolumeRepo(db)
    codes = _all_codes(db)
    if not force:
        done = repo.codes_with_window(want, len(want))
        codes = [x for x in codes if x not in done]

    stat = {"done": 0, "total": len(codes), "ok": 0, "empty": 0, "fail": 0,
            "rows": 0, "failed": [], "days": want}
    log.info("爆量回补开始: %d 只（跳过已完整 %d 只），窗口 %s ~ %s",
             len(codes), len(_all_codes(db)) - len(codes), want[0], want[-1])
    if not codes:
        return stat

    limiter = _RateLimiter(c["backfill_qps"])
    want_set = set(want)
    batch: list[tuple] = []

    def flush() -> None:
        nonlocal batch
        if batch:
            stat["rows"] += repo.save_day_batch(batch)
            batch = []

    with ThreadPoolExecutor(max_workers=c["backfill_workers"]) as ex:
        futs = [ex.submit(_fetch_one, code, start_ms, end_ms, limiter)
                for code in codes]
        for fut in as_completed(futs):
            code, bars = fut.result()
            stat["done"] += 1
            if bars is None:
                stat["fail"] += 1
                if len(stat["failed"]) < 200:
                    stat["failed"].append(code)
            elif not bars:
                stat["empty"] += 1
            else:
                stat["ok"] += 1
                prev = 0.0
                for b in bars:
                    if b["date"] in want_set:
                        batch.append((b["date"], code, b["volume"], b["turnover"],
                                      b["close"], prev))
                    prev = b["close"]
                if len(batch) >= 2000:
                    flush()
            if on_progress and stat["done"] % 50 == 0:
                on_progress(dict(stat))
    flush()
    if on_progress:
        on_progress(dict(stat))
    log.info("爆量回补完成: 成功 %d / 空 %d / 失败 %d，写入 %d 行",
             stat["ok"], stat["empty"], stat["fail"], stat["rows"])
    return stat


def coverage_report(db, cal, trade_date: date, cfg) -> dict:
    """某日所需窗口的覆盖情况，供前端提示「还缺哪几天」。"""
    c = vcfg(cfg)
    window = prev_trading_days(cal, trade_date, c["base_window"])
    repo = VolumeRepo(db)
    cov = repo.coverage(window + [trade_date.isoformat()])
    lack = [d for d in window + [trade_date.isoformat()]
            if cov.get(d, 0) < c["min_total"]]
    return {"window": window, "coverage": cov, "lack": lack,
            "ready": not lack}


# ------------------------------------------------------------------ 任务状态

class SurgeJob:
    """后台回补任务状态。uvicorn 单进程，模块级单例即可。

    重启丢进度，但已写入的 daily_volume 还在，backfill 的
    codes_with_window 会断点续跑。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._th: threading.Thread | None = None
        self._s = {"running": False, "phase": "idle", "done": 0, "total": 0,
                   "ok": 0, "empty": 0, "fail": 0, "rows": 0,
                   "started_at": "", "finished_at": "", "message": ""}

    def running(self) -> bool:
        with self._lock:
            return bool(self._th and self._th.is_alive())

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._s)

    def update(self, stat: dict) -> None:
        with self._lock:
            for k in ("done", "total", "ok", "empty", "fail", "rows"):
                if k in stat:
                    self._s[k] = stat[k]
            self._s["phase"] = "fetch"

    def start(self, fn) -> bool:
        with self._lock:
            if self._th and self._th.is_alive():
                return False
            self._s.update({"running": True, "phase": "start", "done": 0,
                            "total": 0, "ok": 0, "empty": 0, "fail": 0, "rows": 0,
                            "started_at": datetime.datetime.now().strftime("%H:%M:%S"),
                            "finished_at": "", "message": "准备中…"})
            self._th = threading.Thread(target=self._run, args=(fn,), daemon=True)
            self._th.start()
            return True

    def _run(self, fn) -> None:
        try:
            r = fn()
            msg = "完成：成功 %d 只 / 空 %d / 失败 %d，写入 %d 行" % (
                r.get("ok", 0), r.get("empty", 0), r.get("fail", 0), r.get("rows", 0))
            with self._lock:
                self._s.update({"phase": "done", "message": msg})
        except Exception as e:  # noqa: BLE001
            log.exception("爆量回补失败")
            with self._lock:
                self._s.update({"phase": "error", "message": str(e)[:200]})
        finally:
            with self._lock:
                self._s["running"] = False
                self._s["finished_at"] = datetime.datetime.now().strftime("%H:%M:%S")


JOB = SurgeJob()
