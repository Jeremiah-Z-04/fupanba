"""同花顺官方金融数据 API（fuyao.aicubes.cn）数据源 —— 主数据源

优势：涨停池含连板/封单/涨停原因、同花顺概念板块成分（贴合同花顺导入工作流）。
缺口（同花顺快照/池不含换手率、流通市值）：由 pipeline 用东方财富 akshare 池按
code 补字段（见 src/pipeline.py 的 enrich 步骤）。

依赖：src/datasources/fuyao_api.py（内置客户端，零第三方依赖，仅 urllib）。
"""
from __future__ import annotations

import time
from datetime import date

from src.datasources import fuyao_api as fy
from src.datasources.base import (
    ConceptBlock, DataSource, ZTStock, is_bj_code, normalize_code,
)
from src.log import get_logger

log = get_logger(__name__)

_YIZI_TIME = "09:25"  # 集合竞价即涨停 = 一字板


def _fmt_time(raw) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    if ":" in s:
        parts = s.split(":")
        return ":".join(p.zfill(2) for p in parts[:3]) if len(parts) == 2 else s
    s = s.zfill(6)
    return f"{s[:2]}:{s[2:4]}:{s[4:6]}"


def _is_st_name(name: str) -> bool:
    n = (name or "").upper()
    return "ST" in n or "退" in n


class FuyaoSource(DataSource):
    name = "fuyao"

    def __init__(self):
        if not fy.is_configured():
            raise RuntimeError("同花顺 API Key 未配置（.env 缺失 HITHINK_FINANCE_API_KEY）")

    def is_available(self) -> bool:
        return fy.is_available()

    # ------------------------------------------------ 涨停池
    def get_zt_pool(self, trade_date: date) -> list[ZTStock]:
        date_ms = fy.sh_date_ms(trade_date.isoformat())
        items = fy.limit_up_pool(date_ms=date_ms)
        if items is None:
            raise RuntimeError(f"同花顺涨停池拉取失败: {fy.last_error}")

        # 成交额用全市场快照补（快照含 turnover 元，但无换手率/流通市值）。
        # 注意：全市场快照是实时的，仅当天有效；历史日期 amount 留 0，由
        # pipeline._enrich_from_em 用东财历史涨停池补。
        amount_map: dict[str, float] = {}
        if trade_date == date.today():
            amount_map = self._amount_map()
        out: list[ZTStock] = []
        for it in items:
            code = normalize_code(str(it.get("thscode", "")))
            name = str(it.get("name", ""))
            zt_time = _fmt_time(it.get("limit_up_time"))
            out.append(ZTStock(
                code=code, name=name, trade_date=trade_date,
                zt_time=zt_time,
                fengdan_amt=float(it.get("seal_money") or 0),
                amount=amount_map.get(code, 0.0),
                float_mv=0.0,     # 同花顺池无流通市值，由东财补
                turnover=0.0,     # 同花顺池无换手率，由东财补
                high_pct=float(it.get("price_change_ratio_pct") or 0),  # 当日涨幅（10/20cm）
                zhaban_count=0,
                industry="",
                is_st=_is_st_name(name) or bool(it.get("is_st")),
                is_yizi=bool(zt_time) and zt_time <= _YIZI_TIME,
                is_bj=is_bj_code(code),
                lianban_hint=int(it.get("continue_day_cnt") or 0),
                reason=str(it.get("limit_up_reason") or ""),
            ))
        log.info("[fuyao] %s 涨停池 %d 只", trade_date, len(out))
        return out

    def get_zt_pool_zhaban(self, trade_date: date) -> list[ZTStock]:
        date_ms = fy.sh_date_ms(trade_date.isoformat())
        items = fy.limit_break_pool(date_ms=date_ms)
        if items is None:
            log.warning("[fuyao] 炸板池拉取失败: %s", fy.last_error)
            return []
        out: list[ZTStock] = []
        for it in items:
            code = normalize_code(str(it.get("thscode", "")))
            name = str(it.get("name", ""))
            out.append(ZTStock(
                code=code, name=name, trade_date=trade_date,
                zt_time="",
                amount=float(it.get("turnover") or 0),
                float_mv=0.0,
                turnover=float(it.get("turnover_ratio_pct") or 0),
                zhaban_count=int(it.get("open_times") or 1),
                is_st=_is_st_name(name),
                is_bj=is_bj_code(code),
            ))
        log.info("[fuyao] %s 炸板池 %d 只", trade_date, len(out))
        return out

    def _amount_map(self) -> dict[str, float]:
        """全市场快照 code -> 成交额(元)。失败返回空 dict（不致命）。"""
        snap = fy.all_a_share_snapshot()
        if not snap:
            log.warning("[fuyao] 全市场快照拉取失败，成交额因子将缺失")
            return {}
        return {
            normalize_code(t): float(it.get("turnover") or 0)
            for t, it in snap.items()
        }

    def get_strong_pool(self, trade_date: date, exclude_codes=None) -> list[ZTStock]:
        """创业板/科创板涨幅 > 9.90% 且未封板的强势股。

        这两块是 20cm 板，主板 10cm 的涨停阈值不适用：涨幅过 9.90 已经相当于
        主板一个板，虽未封板也要计入题材强度与龙头梯队（用户口径）。
        主板未封板的高涨幅股走炸板池，北交所 30cm 暂不纳入。

        全市场快照是实时的，只能用于最近一个交易日（历史日期无法还原）。
        """
        snap = fy.all_a_share_snapshot()
        if not snap:
            log.warning("[fuyao] 全市场快照失败，异动池为空")
            return []
        if exclude_codes is None:
            exclude_codes = {s.code for s in self.get_zt_pool(trade_date)}
        names = fy.all_a_share_names() or {}
        out: list[ZTStock] = []
        for ticker, it in snap.items():
            code = normalize_code(ticker)
            if not code.startswith(("30", "68")):
                continue  # 只取创业板/科创板（20cm）
            pct = float(it.get("price_change_ratio_pct") or 0)
            if pct <= 9.90:
                continue
            if code in exclude_codes:
                continue  # 已封板涨停，走涨停池
            name = names.get(ticker, "")
            if "ST" in name.upper() or "退" in name:
                continue
            if name.startswith(("N", "C")):  # 新股（N=上市首日，C=上市次日至第5日）
                continue
            out.append(ZTStock(
                code=code, name=name, trade_date=trade_date,
                zt_time="",
                amount=float(it.get("turnover") or 0),
                float_mv=0.0, turnover=0.0,
                high_pct=pct,
                is_st=False, is_bj=False,
            ))
        # 异动原因仅当日有效（跨 0 点清空），历史日期拿不到，跳过
        if out and trade_date == date.today():
            self._fill_anomaly_reason(out)
        out.sort(key=lambda s: -s.high_pct)
        log.info("[fuyao] %s 创/科异动池（涨幅>9.9%%未封板）%d 只", trade_date, len(out))
        return out

    def _fill_anomaly_reason(self, stocks: list[ZTStock], batch_size: int = 50) -> None:
        """用同花顺异动原因接口补 reason / anomaly_tag / anomaly_content。

        keyword_list 是官方提炼的结构化题材关键词（如 ["芯片", "半导体"]），
        用 "+" 拼接写入 reason 走现有规则归堆；不用 analysis_content 整句
        （会产生长句垃圾锚点），原文单独存 anomaly_content 供复盘查看。
        接口仅当日有效，过 0 点清空，因此必须收盘后当天采集落库。
        单批上限 50 只，失败降级为不填，不影响主链路。
        """
        codes = [fy.to_thscode(s.code) for s in stocks]
        codes = [c for c in codes if c]
        if not codes:
            return
        try:
            res = fy.anomaly_analysis_stock(codes)
        except Exception as e:
            log.warning("[fuyao] 异动原因拉取异常: %s", e)
            return
        if not res:
            log.info("[fuyao] 异动原因接口无数据（当日未生成或限流），跳过补全")
            return
        filled = 0
        for s in stocks:
            item = res.get(fy.to_thscode(s.code))
            if not item:
                continue
            kws = item.get("keyword_list") or []
            reason = "+".join(str(k).strip() for k in kws if str(k).strip())
            if not s.reason and reason:
                s.reason = reason
            if not s.anomaly_tag:
                s.anomaly_tag = str(item.get("tag_name") or "")
            if not s.anomaly_content:
                s.anomaly_content = str(item.get("analysis_content") or "")
            if reason or s.anomaly_content:
                filled += 1
        log.info("[fuyao] 异动原因补全 %d/%d 只（含 tag/解读原文）", filled, len(stocks))

    # ------------------------------------------------ 概念板块
    def get_concept_blocks(self) -> list[ConceptBlock]:
        concepts = fy.ths_index_list(tag="cn_concept")
        if not concepts:
            raise RuntimeError(f"同花顺概念列表拉取失败: {fy.last_error}")
        out: list[ConceptBlock] = []
        for i, c in enumerate(concepts):
            thscode, name = c.get("thscode"), c.get("name")
            if not thscode or not name:
                continue
            try:
                cons = fy.ths_index_constituents(thscode)
                if not cons:
                    continue
                codes = [
                    normalize_code(str(x.get("ticker") or x.get("thscode") or ""))
                    for x in cons
                ]
                codes = [c for c in codes if c]
                if codes:
                    out.append(ConceptBlock(name=str(name), codes=codes, source=self.name))
            except Exception as e:
                log.warning("概念[%s]成分拉取失败: %s", name, e)
            time.sleep(0.15)  # 限速
            if (i + 1) % 50 == 0:
                log.info("同花顺概念成分进度 %d/%d", i + 1, len(concepts))
        log.info("[fuyao] 同花顺概念板块 %d 个，成分拉取完成", len(out))
        return out
