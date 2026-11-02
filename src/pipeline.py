"""盘后主管线：拉取涨停池 → 落库 → 连板更新 → 聚类 → 龙头排序 → 导出

CLI 与 Web 服务共用本模块，保证两条入口行为完全一致。
"""
from __future__ import annotations

from datetime import date, datetime

from src.config import Config
from src.core.clustering.static_concept import StaticConceptClusterer
from src.core.clustering.reason_clusterer import ReasonClusterer
from src.core.leader_rank import LeaderRanker
from src.core.lianban import LianbanTracker
from src.core.trading_calendar import get_calendar
from src.datasources.router import DataSourceRouter
from src.export.clipboard import build_code_list, copy_to_clipboard
from src.export.csv_report import export_csv
from src.log import get_logger
from src.storage.db import DB
from src.storage.snapshot_repo import SnapshotRepo

log = get_logger(__name__)

# akshare 是否可用（模块级缓存）。回填 100+ 天时每天都会走 _enrich_from_em，
# 不缓存的话会抛 100+ 次同一个 ImportError，日志被刷满还拖慢回填。
_EM_AVAILABLE: bool | None = None


def em_available() -> bool:
    global _EM_AVAILABLE
    if _EM_AVAILABLE is None:
        try:
            import akshare  # noqa: F401
            _EM_AVAILABLE = True
        except Exception:
            _EM_AVAILABLE = False
            log.info("akshare 未安装，封单占比/换手率/流通市值将缺省（显示 -）")
    return _EM_AVAILABLE


class Pipeline:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config.load()
        self.db = DB(self.cfg.db_path)
        self.repo = SnapshotRepo(self.db)
        self.router = DataSourceRouter(self.cfg)
        self.cal = get_calendar(self.cfg.data_dir)
        # 与 LianbanTracker 同一口径：ST 股是否剔除出连板梯队
        self.st_excluded = bool(self.cfg.get("lianban.st_not_counted", True))

    # ------------------------------------------------ 步骤 0：休市回退
    def _resolve_day(self, trade_date: date) -> dict:
        """休市日（周末/节假日）自动回退到上一交易日。

        接口在非交易日返回空池，直接采集会把「休市」误判成「0 涨停」，
        还会污染连板状态。这里统一把请求日映射成实际数据日。
        """
        info = self.cal.resolve(trade_date)
        if info["adjusted"]:
            kind = "周末" if info["holiday_kind"] == "weekend" else "节假日"
            log.info("[%s] %s休市，改用上一交易日 %s 的数据",
                     trade_date, kind, info["trade_date"])
        return info

    # ------------------------------------------------ 步骤 1：采集
    def fetch(self, trade_date: date) -> dict:
        """拉取当日涨停池+炸板池，落库并更新连板状态（休市日自动用上一交易日）"""
        day = self._resolve_day(trade_date)
        trade_date = day["trade_date"]
        from src.core.market import market_status
        status = market_status(trade_date)
        if status == "future":
            raise ValueError(f"{trade_date} 是未来日期，无法采集")
        if status == "holiday":
            raise ValueError(f"{trade_date} 休市（无交易日历可回退），无法采集")
        if status == "preopen":
            raise ValueError(
                f"{trade_date} 尚未开盘（盘前涨停池无当日数据），请盘后采集"
            )
        is_intraday = status == "intraday"
        if is_intraday:
            log.warning("[%s] 盘中快照：涨停数据收盘前会变化，收盘后请重新采集", trade_date)

        zt = self.router.get_zt_pool(trade_date)
        zb = self.router.get_zt_pool_zhaban(trade_date)

        # 归纳（聚类）只用同花顺涨停原因；流通市值/换手率同花顺不提供，
        # 受限时用东财 akshare 池按 code 补字段（best-effort，失败则因子缺省）
        if self.router.active.name == "fuyao":
            self._enrich_from_em(zt, trade_date)

        exclude_bj = self.cfg.get("zt_pool.exclude_bj", False)
        if exclude_bj:
            zt = [s for s in zt if not s.is_bj]
            zb = [s for s in zb if not s.is_bj]

        # 注意：连板判定所需的"历史状态/交易日历"必须在落库前取，
        # 否则当日数据入库后 known_dates 非空，首次运行的种子机制永远不触发
        prev_state = self.repo.load_lianban_state()
        known_dates = [
            r["trade_date"]
            for r in self.db.query(
                "SELECT DISTINCT trade_date FROM daily_zt_pool ORDER BY trade_date"
            )
        ]

        # 创/科异动池是全市场实时快照，只有「最近一个交易日」有效：
        # 周末/节假日打开时，快照仍是上一个交易日收盘价，可以正常用；
        # 更早的历史日期无法还原，硬采只会把今天的快照按到历史日期上。
        strong: list = []
        latest_day = self.cal.latest_trading_day(date.today())
        if trade_date == latest_day:
            strong = self.router.get_strong_pool(
                trade_date, exclude_codes={s.code for s in zt}
            )
            if exclude_bj:
                strong = [s for s in strong if not s.is_bj]
        else:
            log.info("[%s] 非最近交易日（%s），跳过创/科异动池（实时快照不可还原）",
                     trade_date, latest_day)

        # 异动原因/解读仅当日有效（同花顺过 0 点清空，历史无法补回），重采时
        # 若接口失败（限流/3002/网络抖动）会把已存的原因冲成空值 ——
        # 删除前先缓存库里已有的非空值，落库时补回
        prev_anomaly: dict[str, dict] = {}
        if trade_date == latest_day:
            prev_anomaly = {
                r["code"]: {"reason": r["reason"], "tag": r["anomaly_tag"],
                            "content": r["anomaly_content"]}
                for r in self.db.query(
                    "SELECT code, reason, anomaly_tag, anomaly_content "
                    "FROM daily_zt_pool WHERE trade_date=? AND sealed=2 "
                    "AND (reason<>'' OR anomaly_content<>'')",
                    (trade_date.isoformat(),))
            }

        # 全量刷新：先删当日旧数据，避免上次「盘中/午休快照」的残留在库里。
        # 异动池（sealed=2）只在最近交易日采集，历史重采时 strong 恒空，
        # 若一并删除会让历史异动股整行永久丢失 —— 故历史日只删涨停/炸板池。
        if trade_date == latest_day:
            removed = self.repo.delete_zt_pool(trade_date)
        else:
            removed = (self.repo.delete_zt_pool(trade_date, sealed=1)
                       + self.repo.delete_zt_pool(trade_date, sealed=0))
        if removed:
            log.info("[%s] 重采：清理旧池数据 %d 行", trade_date, removed)

        src_name = self.router.active.name
        self.repo.save_zt_pool(zt, sealed=1, source=src_name)
        self.repo.save_zt_pool(zb, sealed=0, source=src_name)
        # 接口没拿到的字段用缓存补回，避免覆盖式写入丢数据
        for s in strong:
            prev = prev_anomaly.get(s.code)
            if not prev:
                continue
            if not s.reason and prev["reason"]:
                s.reason = prev["reason"]
            if not s.anomaly_tag and prev["tag"]:
                s.anomaly_tag = prev["tag"]
            if not s.anomaly_content and prev["content"]:
                s.anomaly_content = prev["content"]
        if strong:
            self.repo.save_zt_pool(strong, sealed=2, source=src_name)

        # 连板：库里已有更晚日期时（历史重采），当前 lianban_state 属于更晚的日期，
        # 拿它推导只会把当日全部打成首板 —— 改用数据源自带连板数。
        today_iso = trade_date.isoformat()
        latest_known = known_dates[-1] if known_dates else ""
        is_latest = (not latest_known) or today_iso >= latest_known
        if is_latest:
            tracker = LianbanTracker(self.cfg)
            new_state = tracker.update(trade_date, zt, prev_state, known_dates)
            new_state = self._reconcile_heights(new_state, zt, trade_date)
            self.repo.save_lianban_state(new_state)
        else:
            new_state = [
                {"code": s.code, "name": s.name,
                 "height": max(int(s.lianban_hint or 0), 1),
                 "last_zt_date": today_iso,
                 "yizi_count": 1 if s.is_yizi else 0}
                for s in zt if not (self.st_excluded and s.is_st)
            ]
            new_state.sort(key=lambda x: -x["height"])
            log.info("[%s] 历史重采（库中已有更晚的 %s），连板高度直接用数据源连板数",
                     trade_date, latest_known)

        # 回写自维护连板高度到 daily_zt_pool（当天用自维护口径，
        # 历史回填则保留 continue_day_cnt 直接写）
        for s in new_state:
            self.db.execute(
                "UPDATE daily_zt_pool SET lianban_height=? "
                "WHERE trade_date=? AND code=? AND sealed=1",
                (s["height"], trade_date.isoformat(), s["code"]),
            )

        log.info("[%s] 采集完成: 涨停 %d 只, 炸板 %d 只, 异动 %d 只, 连板状态 %d 只",
                 trade_date, len(zt), len(zb), len(strong), len(new_state))
        self._save_breadth(trade_date, len(zt))
        self._save_volume(trade_date, is_intraday)
        self._mark_snapshot(trade_date, is_intraday)
        return {
            "zt": len(zt), "zhaban": len(zb), "strong": len(strong),
            "lianban": len(new_state), "is_intraday": is_intraday,
            "is_snapshot": is_intraday,
            # 请求日 vs 实际数据日：休市日两者不同，前端据此提示「休市中」
            "requested_date": day["requested"].isoformat(),
            "trade_date": trade_date.isoformat(),
            "is_holiday": day["adjusted"],
            "holiday_kind": day["holiday_kind"],
            "day_label": day["label"],
        }

    def _reconcile_heights(self, state: list[dict], zt: list, trade_date: date) -> list[dict]:
        """连板高度纠偏：自维护状态低于数据源连板数时，以数据源为准。

        自维护依赖「上一交易日的状态表」，一旦某天漏跑 / 重采顺序错乱 / 上次采到
        的是盘中快照，状态就会低估甚至全部打成 1 板。数据源的 continue_day_cnt
        是同一口径的连板数，天然可以做校验：只向上修正，不向下覆盖。
        """
        hints = {s.code: int(getattr(s, "lianban_hint", 0) or 0) for s in zt}
        fixed: list[str] = []
        for row in state:
            hint = hints.get(row["code"], 0)
            if hint > row["height"]:
                fixed.append(f"{row['code']}:{row['height']}->{hint}")
                row["height"] = hint
        if fixed:
            log.warning("[%s] 连板高度纠偏 %d 只（自维护状态低于数据源）: %s",
                        trade_date, len(fixed), ",".join(fixed[:10]))
        return state

    def _mark_snapshot(self, trade_date: date, is_snapshot: bool) -> None:
        """记录该日数据是否来自盘中快照。

        盘中采到的涨停池只有半天数据（午休时甚至只有早盘），收盘后必须重采；
        打上标记后前端可在日期旁提示「盘中快照」，避免把半日数据当收盘结果。
        """
        key = f"snapshot:{trade_date.isoformat()}"
        if is_snapshot:
            self.db.execute(
                "INSERT OR REPLACE INTO meta VALUES (?,?)",
                (key, datetime.now().isoformat(timespec="seconds")),
            )
        else:
            self.db.execute("DELETE FROM meta WHERE key=?", (key,))

    def _save_breadth(self, trade_date: date, limit_up: int) -> None:
        """落库全市场情绪宽度（涨幅>=9.9 / 涨停 / 创科北>10）。

        全市场快照是实时的，只有「最近一个交易日」能算。历史回填时必须跳过，
        否则会把今天的宽度挂到几个月前的日期上。
        """
        try:
            latest = self._resolve_day(date.today())["trade_date"]
        except Exception:
            latest = trade_date
        if trade_date != latest:
            return
        try:
            from src.core import breadth
            from src.datasources import fuyao_api as fy

            snap = fy.all_a_share_snapshot()
            if not snap:
                log.warning("[%s] 全市场快照为空，跳过情绪宽度", trade_date)
                return
            b = breadth.compute(snap)
            if not b.get("total"):
                return
            self.db.execute(
                "INSERT OR REPLACE INTO market_breadth "
                "(trade_date, up_99, limit_up, tech_10, total) VALUES (?,?,?,?,?)",
                (trade_date.isoformat(), b["up_99"], limit_up, b["tech_10"], b["total"]),
            )
            log.info("[%s] 情绪宽度: 涨幅>=9.9 %d 只 / 涨停 %d 只 / 创科北>10%% %d 只（快照 %d 只）",
                     trade_date, b["up_99"], limit_up, b["tech_10"], b["total"])
        except Exception as e:                       # 宽度失败不能影响采集主流程
            log.warning("[%s] 情绪宽度计算失败: %s", trade_date, e)

    def _save_volume(self, trade_date: date, is_intraday: bool) -> None:
        """把全市场快照的成交量落到 daily_volume（0 额外请求）。

        快照是实时的，只有「最近一个交易日」能落；历史日靠个股日K回补
        （main.py volfill）。紧跟 _save_breadth 调用，25s TTL 缓存命中，
        不会重复打接口。
        """
        try:
            latest = self._resolve_day(date.today())["trade_date"]
        except Exception:
            latest = trade_date
        if trade_date != latest:
            return
        try:
            from src.datasources import fuyao_api as fy
            from src.storage.volume_repo import VolumeRepo

            snap = fy.all_a_share_snapshot()
            if not snap:
                log.warning("[%s] 全市场快照为空，跳过成交量落库", trade_date)
                return
            min_total = int(self.cfg.get("volume.min_total", 4000))
            if len(snap) < min_total:
                log.warning("[%s] 快照只有 %d 只（<%d），视为不完整，不落库",
                            trade_date, len(snap), min_total)
                return
            rows = []
            for ticker, it in snap.items():
                code = str(ticker).split(".")[0]
                try:
                    rows.append((
                        code,
                        float(it.get("volume") or 0),
                        float(it.get("turnover") or 0),
                        float(it.get("last_price") or 0),
                        float(it.get("prev_price") or 0),
                    ))
                except (TypeError, ValueError):
                    continue
            n = VolumeRepo(self.db).save_day(
                trade_date.isoformat(), rows,
                is_snapshot=1 if is_intraday else 0, source="snapshot")
            log.info("[%s] 全市场成交量落库 %d 行（快照 %d 只%s）",
                     trade_date, n, len(snap), "，盘中不完整" if is_intraday else "")
        except Exception as e:                       # 落量失败不能影响采集主流程
            log.warning("[%s] 成交量落库失败（不影响采集）: %s", trade_date, e)

    def _enrich_from_em(self, zt: list, trade_date: date) -> int:
        """东财补字段：流通市值、换手率（成交额已有，全市场快照已补）。

        同花顺涨停池/快照均不含这两个字段，而「封单流通占比」「合理换手」是
        龙头排序的核心因子，必须保留东财来源补全。失败不影响主链路（因子缺省=0）。

        返回东财池的股票数（0 = 该日期东财没数据，回填时用它做熔断）。
        """
        if not em_available():
            return 0
        try:
            from src.datasources.akshare_ds import AkshareSource

            em = AkshareSource()
            em_pool = {s.code: s for s in em.get_zt_pool(trade_date)}
            for s in zt:
                em_s = em_pool.get(s.code)
                if em_s:
                    if s.float_mv <= 0:
                        s.float_mv = em_s.float_mv
                    if s.turnover <= 0:
                        s.turnover = em_s.turnover
                    if s.amount <= 0:
                        s.amount = em_s.amount
            log.info("[enrich] 东财补字段完成，匹配 %d/%d", len(em_pool), len(zt))
            return len(em_pool)
        except Exception as e:
            log.warning("[enrich] 东财补字段失败（因子将缺省）: %s", e)
            return 0

    # ------------------------------------------------ 步骤 2：概念缓存
    def refresh_concepts(self, force: bool = False) -> int:
        """周更概念成分。force=True 强制刷新"""
        updated = self.repo.concept_updated_at()
        if updated and not force:
            last = datetime.fromisoformat(updated)
            if (datetime.now() - last).days < 7:
                log.info("概念成分 %s 更新过，跳过（7 天内有效）", updated)
                return -1
        blocks = self.router.get_concept_blocks()
        self.repo.rebuild_concepts(blocks)
        return len(blocks)

    # ------------------------------------------------ 历史回填
    def backfill(self, days: int = 30) -> dict:
        """回填最近 N 个交易日的涨停/炸板池并跑复盘。

        历史涨停池自带 continue_day_cnt（连板数）和 limit_up_reason（涨停原因），
        直接落库为 lianban_height / reason，无需逐日推导连板。
        """
        from src.datasources import fuyao_api as fy
        from src.datasources.fuyao_ds import FuyaoSource

        trading = fy.trading_days()
        if not trading:
            raise RuntimeError("同花顺交易日历获取失败")
        today = date.today().strftime("%Y%m%d")
        # 只回填「早于今天」的已收盘交易日；今天数据由盘后手动 fetch 采集，
        # 否则未开盘时拉今天会拿到上一交易日的部分数据（脏数据）
        targets = [d for d in trading if d < today][-days:]

        src = FuyaoSource()
        done, skipped, failed, empty = [], 0, [], []
        # 东财涨停池只保留最近两三个交易日，历史日期必然返空。
        # 连续 3 天拿不到就熔断，否则回填 100+ 天要白等 100+ 次请求。
        em_miss, em_off = 0, False
        for d_str in targets:
            d = date(int(d_str[:4]), int(d_str[4:6]), int(d_str[6:8]))
            if self.repo.has_date(d):
                skipped += 1
                continue
            try:
                zt = src.get_zt_pool(d)
                zb = src.get_zt_pool_zhaban(d)
                if not zt:
                    empty.append(d_str)
                    log.info("[backfill] %s 无涨停数据，跳过", d)
                    continue
                if not em_off:
                    if self._enrich_from_em(zt, d) > 0:
                        em_miss = 0
                    else:
                        em_miss += 1
                        if em_miss >= 3:
                            em_off = True
                            log.info("[backfill] 东财历史池连续 %d 天无数据"
                                     "（只保留最近几日），后续跳过因子补全", em_miss)
                self.repo.save_zt_pool(zt, sealed=True, source="fuyao")
                self.repo.save_zt_pool(zb, sealed=False, source="fuyao")
                self._analyze(d, to_clipboard=False)
                done.append(d_str)
                log.info("[backfill] %s 完成：涨停 %d 只，炸板 %d 只", d, len(zt), len(zb))
            except Exception as e:
                # 失败必须留痕：一次补 100+ 天，只靠日志翻不出漏了哪几天
                failed.append({"date": d_str, "error": str(e)[:200]})
                log.error("[backfill] %s 失败: %s", d, e)
        log.info("[backfill] 完成 %d 个交易日（跳过已有 %d，空 %d，失败 %d，共 %d）",
                 len(done), skipped, len(empty), len(failed), len(targets))
        return {"done": done, "skipped": skipped, "empty": empty,
                "failed": failed, "total": len(targets)}

    # ------------------------------------------------ 步骤 3：复盘计算
    def run(self, trade_date: date, to_clipboard: bool = True) -> dict:
        """盘后复盘主流程：采集（如缺）→ 聚类 → 龙头排序 → 落库 → 导出

        休市日自动使用上一交易日的数据（请求日与数据日在返回值里分开给出）。
        """
        day = self._resolve_day(trade_date)
        trade_date = day["trade_date"]
        from src.core.market import market_status
        status = market_status(trade_date)
        if status == "future":
            raise ValueError(f"{trade_date} 是未来日期，无法复盘")
        if status == "holiday":
            raise ValueError(f"{trade_date} 休市（无交易日历可回退），无法复盘")
        if not self.repo.has_date(trade_date):
            log.info("库中无 %s 数据，先执行采集", trade_date)
            self.fetch(trade_date)
            status = market_status(trade_date)
        if status == "intraday":
            log.warning("[%s] 当前盘中，结果基于盘中快照，收盘后请重新复盘", trade_date)
        result = self._analyze(trade_date, to_clipboard)
        result["is_intraday"] = status == "intraday"
        result.update({
            "requested_date": day["requested"].isoformat(),
            "trade_date": trade_date.isoformat(),
            "is_holiday": day["adjusted"],
            "holiday_kind": day["holiday_kind"],
            "day_label": day["label"],
        })
        return result

    def analyze_day(self, trade_date: date) -> dict:
        """按当前配置重算单日题材（不采集、不动行情数据、不进剪贴板）。

        供「题材框架改动后立即生效」使用：改完框架调一次，页面结果立刻更新。
        """
        return self._analyze(trade_date, to_clipboard=False)

    def reanalyze(self, days: int = 0, force: bool = False) -> dict:
        """用当前配置重算历史题材（只重聚类/排序，不动行情数据、不采集）。

        改 clustering 口径（theme_families / 停用词 / min_cluster_size）之后必须跑一次，
        否则历史日期还是旧口径，只有当天生效。
        默认跳过存在生效人工修正（is_override=1）的日期，避免覆盖人工调整。
        """
        dates = [r["trade_date"] for r in self.db.query(
            "SELECT DISTINCT trade_date FROM daily_zt_pool ORDER BY trade_date")]
        if days and days > 0:
            dates = dates[-days:]
        done: list[str] = []
        skipped: list[str] = []
        failed: list[dict] = []
        for ds in dates:
            if not force:
                n = self.db.query(
                    "SELECT COUNT(*) AS c FROM leader_result "
                    "WHERE trade_date=? AND is_override=1", (ds,))[0]["c"]
                if n:
                    skipped.append(ds)
                    log.info("%s 有 %d 条人工修正，跳过（如需覆盖用 --force）", ds, n)
                    continue
            try:
                self._analyze(date.fromisoformat(ds), to_clipboard=False)
                done.append(ds)
            except Exception as e:
                log.exception("%s 重算失败", ds)
                failed.append({"date": ds, "error": str(e)})
        log.info("历史重算完成: %d 个交易日（跳过 %d，失败 %d）",
                 len(done), len(skipped), len(failed))
        return {"done": done, "skipped": skipped, "failed": failed, "total": len(dates)}

    def _analyze(self, trade_date: date, to_clipboard: bool = True) -> dict:
        """聚类 → 龙头排序 → 落库 → 导出（不负责采集；不做强度打分）"""
        stocks = self.repo.load_zt_pool(trade_date, sealed=1)   # 封板涨停
        strong = self.repo.load_zt_pool(trade_date, sealed=2)   # 创/科异动
        zhaban = self.repo.load_zt_pool(trade_date, sealed=0)   # 炸板
        if not stocks and not strong:
            log.warning("%s 涨停池为空，终止复盘", trade_date)
            return {"themes": {}, "themes_count": 0, "leaders": [], "exported": 0,
                    "csv": "", "codes": "", "clipboard": False}

        # 连板高度已在 daily_zt_pool.lianban_height（当天 fetch 回写自维护值，
        # 历史 backfill 写入 continue_day_cnt）；异动/炸板股无连板
        for s in stocks:
            if not s.get("lianban_height"):
                s["lianban_height"] = 1
        for s in strong:
            s["lianban_height"] = 0
        for s in zhaban:
            s["lianban_height"] = 0

        # 聚类（reason=涨停原因资金逻辑主路径 / static_concept=静态概念兜底）
        mode = self.cfg.get("clustering.mode", "reason")
        if mode == "static_concept":
            clusterer = StaticConceptClusterer(self.cfg, self.repo)
        else:
            clusterer = ReasonClusterer(self.cfg, self.repo)
        clusters = clusterer.cluster(stocks + strong, zhaban)

        # 龙头排序（全部题材、全部成员都存库）
        ranker = LeaderRanker(self.cfg)
        theme_stats: dict[str, dict] = {}
        leaders: list[dict] = []       # 带龙标的（每题材 top_n），供 CSV
        all_rows: list[dict] = []      # 全部成员，供剪贴板导出
        self.db.execute("DELETE FROM theme_result WHERE trade_date=?",
                        (trade_date.isoformat(),))
        self.db.execute("DELETE FROM leader_result WHERE trade_date=?",
                        (trade_date.isoformat(),))
        for theme, members in clusters.items():
            zt_count = sum(1 for s in members if s.get("sealed", 1) == 1)
            strong_count = len(members) - zt_count
            ranked = ranker.rank(members)
            self.db.execute(
                "INSERT OR REPLACE INTO theme_result "
                "(trade_date, theme_name, zt_count, strong_count, member_count, "
                "cluster_mode) VALUES (?,?,?,?,?,?)",
                (trade_date.isoformat(), theme, zt_count, strong_count,
                 len(members), clusterer.name),
            )
            theme_stats[theme] = {
                "zt_count": zt_count,
                "strong_count": strong_count,
                "member_count": len(members),
            }
            for r in ranked:
                self.db.execute(
                    "INSERT OR REPLACE INTO leader_result "
                    "(trade_date, theme_name, code, name, algo_rank, final_rank, "
                    "lianban_height, fengdan_amt, float_mv, zt_time, turnover, "
                    "high_pct, is_override, leader_label, sub_tags) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                    (trade_date.isoformat(), theme, r["code"], r["name"],
                     r["leader_rank"], r["leader_rank"],
                     r.get("lianban_height", 1), r.get("fengdan_amt", 0.0),
                     r.get("float_mv", 0.0), r.get("zt_time", ""),
                     r.get("turnover", 0.0), r.get("high_pct", 0.0),
                     r["leader_label"], r.get("sub_tags", "")),
                )
                row = {**r, "theme_name": theme}
                all_rows.append(row)
                if r["leader_label"]:
                    leaders.append(row)

        theme_stats = dict(sorted(theme_stats.items(),
                                  key=lambda kv: (-kv[1]["member_count"], -kv[1]["zt_count"])))
        log.info("聚类完成: %d 个题材（涨停+异动降序）: %s", len(theme_stats),
                 ", ".join(f"{k}:{v['zt_count']}" for k, v in list(theme_stats.items())[:8]))

        # 爆量题材：跟着复盘一起刷新（改了题材框架重算时也能一并更新）
        try:
            from src.core import volume_surge as vs
            vr = vs.scan_day(self.cfg, self.db, self.cal, trade_date)
            if vr.get("ok"):
                log.info("[%s] 爆量题材: %d 只命中 → %d 个板块",
                         trade_date, vr.get("show", 0), vr.get("themes", 0))
            else:
                log.info("[%s] 跳过爆量扫描: %s", trade_date, vr.get("reason"))
        except Exception as e:                       # 爆量失败不能影响复盘主流程
            log.warning("[%s] 爆量扫描失败（不影响复盘）: %s", trade_date, e)

        # 导出：CSV 是龙标报表；剪贴板给「所有题材的全部成分股」
        csv_path = export_csv(leaders, trade_date, self.cfg)
        code_text = build_code_list(all_rows, self.cfg)
        n_codes = len({r["code"] for r in all_rows})
        clip_ok = False
        if to_clipboard and code_text:
            try:
                copy_to_clipboard(code_text)
                clip_ok = True
            except Exception as e:
                log.error("剪贴板写入失败（可手动复制 CSV）: %s", e)

        return {
            "themes": theme_stats,
            "themes_count": len(theme_stats),
            "leaders": leaders,
            "csv": str(csv_path),
            "codes": code_text,
            "exported": n_codes,
            "clipboard": clip_ok,
        }
