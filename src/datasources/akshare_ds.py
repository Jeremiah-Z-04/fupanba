"""akshare 数据源（备份主用皆宜）：东方财富涨停池接口

依赖: pip install akshare
接口:
  - ak.stock_zt_pool_em(date)      涨停股池：首次/最后封板时间、封板资金、炸板次数、连板数
  - ak.stock_zt_pool_zbgc_em(date) 炸板股池
  - ak.stock_board_concept_name_em()      概念板块列表
  - ak.stock_board_concept_cons_em(symbol) 概念成分
"""
from __future__ import annotations

import time
from datetime import date

from src.datasources.base import (
    ConceptBlock, DataSource, ZTStock, is_bj_code, normalize_code,
)
from src.log import get_logger

log = get_logger(__name__)

# 首次封板时间经 _fmt_time 格式化后与该阈值比较（09:30 前封板视为一字候选）
_YIZI_TIME_THRESHOLD = "09:30:00"


def _fmt_time(raw: str) -> str:
    """'092500' -> '09:25:00'；已是 'HH:MM:SS' 则原样返回"""
    raw = str(raw).strip()
    if ":" in raw:
        return raw
    raw = raw.zfill(6)
    return f"{raw[:2]}:{raw[2:4]}:{raw[4:6]}"


def _is_st_name(name: str) -> bool:
    n = name.upper()
    return "ST" in n or "退" in name


class AkshareSource(DataSource):
    name = "akshare"

    def __init__(self):
        try:
            import akshare  # noqa: F401
            self._ak = akshare
        except ImportError as e:
            raise RuntimeError("akshare 未安装: pip install akshare") from e

    def is_available(self) -> bool:
        try:
            df = self._ak.stock_board_concept_name_em()
            return df is not None and not df.empty
        except Exception as e:
            log.warning("akshare 探活失败: %s", e)
            return False

    # ------------------------------------------------ 涨停池
    def get_zt_pool(self, trade_date: date) -> list[ZTStock]:
        df = self._ak.stock_zt_pool_em(date=trade_date.strftime("%Y%m%d"))
        if df is None or df.empty:
            log.info("[%s] %s 涨停池为空（非交易日或当日无涨停）", self.name, trade_date)
            return []
        out: list[ZTStock] = []
        for _, r in df.iterrows():
            code = normalize_code(str(r.get("代码", "")))
            name = str(r.get("名称", ""))
            first_zt = _fmt_time(r.get("首次封板时间", ""))
            zhaban = int(r.get("炸板次数", 0) or 0)
            out.append(ZTStock(
                code=code,
                name=name,
                trade_date=trade_date,
                zt_time=first_zt,
                last_zt_time=_fmt_time(r.get("最后封板时间", "")),
                fengdan_amt=float(r.get("封板资金", 0) or 0),
                amount=float(r.get("成交额", 0) or 0),
                float_mv=float(r.get("流通市值", 0) or 0),
                turnover=float(r.get("换手率", 0) or 0),
                zhaban_count=zhaban,
                high_pct=float(r.get("涨跌幅", 0) or 0),
                industry=str(r.get("所属行业", "")),
                is_st=_is_st_name(name),
                is_yizi=bool(first_zt) and first_zt <= _YIZI_TIME_THRESHOLD and zhaban == 0,
                is_bj=is_bj_code(code),
                lianban_hint=int(r.get("连板数", 0) or 0),
            ))
        log.info("[%s] %s 涨停池 %d 只", self.name, trade_date, len(out))
        return out

    def get_zt_pool_zhaban(self, trade_date: date) -> list[ZTStock]:
        try:
            df = self._ak.stock_zt_pool_zbgc_em(date=trade_date.strftime("%Y%m%d"))
        except Exception as e:
            log.warning("[%s] 炸板池拉取失败: %s", self.name, e)
            return []
        if df is None or df.empty:
            return []
        out: list[ZTStock] = []
        for _, r in df.iterrows():
            code = normalize_code(str(r.get("代码", "")))
            name = str(r.get("名称", ""))
            high_pct = float(r.get("涨跌幅", 0) or 0)  # 炸板池中为当前涨幅
            zt_price_pct = float(r.get("涨停价", 0) or 0)
            # 回落幅度 ≈ 涨停价-现价（接口给出价格字段时）
            out.append(ZTStock(
                code=code, name=name, trade_date=trade_date,
                zt_time=_fmt_time(r.get("首次封板时间", "")),
                amount=float(r.get("成交额", 0) or 0),
                float_mv=float(r.get("流通市值", 0) or 0),
                turnover=float(r.get("换手率", 0) or 0),
                zhaban_count=1,
                high_pct=high_pct,
                is_st=_is_st_name(name),
                is_bj=is_bj_code(code),
            ))
        log.info("[%s] %s 炸板池 %d 只", self.name, trade_date, len(out))
        return out

    # ------------------------------------------------ 概念板块
    _EM_LIST_API = "https://push2.eastmoney.com/api/qt/clist/get"
    _HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    def _em_clist(self, fs: str, fields: str, page_size: int = 500) -> list[dict]:
        """东财 clist 通用分页拉取（带浏览器 UA，避免代理断连）"""
        import requests

        out: list[dict] = []
        page = 1
        while True:
            resp = requests.get(self._EM_LIST_API, headers=self._HEADERS, timeout=15, params={
                "pn": page, "pz": page_size, "po": 1, "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2, "fid": "f12", "fs": fs, "fields": fields,
            })
            resp.raise_for_status()
            data = resp.json().get("data")
            if not data or not data.get("diff"):
                break
            diff = data["diff"]
            out.extend(diff if isinstance(diff, list) else list(diff.values()))
            if len(out) >= data.get("total", 0):
                break
            page += 1
            time.sleep(0.3)
        return out

    def get_concept_blocks(self) -> list[ConceptBlock]:
        # 概念板块列表（fs: m:90 t:3 f:!50 为东财概念板块全集）
        boards = self._em_clist("m:90 t:3 f:!50", "f12,f14")
        if not boards:
            raise RuntimeError("概念板块列表为空")
        out: list[ConceptBlock] = []
        for i, b in enumerate(boards):
            bk_code, name = b["f12"], str(b["f14"])
            try:
                cons = self._em_clist(f"b:{bk_code}", "f12", page_size=1000)
                codes = [normalize_code(c["f12"]) for c in cons]
                out.append(ConceptBlock(name=name, codes=codes, source=self.name))
            except Exception as e:
                log.warning("概念[%s]成分拉取失败: %s", name, e)
            time.sleep(0.3)  # 限速，避免封 IP
            if (i + 1) % 50 == 0:
                log.info("概念成分进度 %d/%d", i + 1, len(boards))
        log.info("[%s] 概念板块 %d 个，成分拉取完成", self.name, len(out))
        return out
