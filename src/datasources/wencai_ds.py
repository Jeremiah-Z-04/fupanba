"""pywencai 问财数据源（可选，默认不启用）

注意: pywencai 依赖 Node.js 环境（内部执行 js），分发给朋友时不作为默认源。
接口策略随问财改版会失效，遇到失败请先 pip install -U pywencai。
"""
from __future__ import annotations

from datetime import date

from src.datasources.base import (
    ConceptBlock, DataSource, ZTStock, is_bj_code, normalize_code,
)
from src.datasources.ifind_ds import _pick, _to_float
from src.log import get_logger

log = get_logger(__name__)


class WencaiSource(DataSource):
    name = "wencai"

    def __init__(self):
        try:
            import pywencai
            self._wc = pywencai
        except ImportError as e:
            raise RuntimeError(
                "pywencai 未安装（需要 Node.js 环境）: pip install pywencai"
            ) from e

    def is_available(self) -> bool:
        try:
            r = self._wc.get(query="上证指数", loop=False)
            return r is not None
        except Exception as e:
            log.warning("pywencai 探活失败: %s", e)
            return False

    def get_zt_pool(self, trade_date: date) -> list[ZTStock]:
        d = trade_date.strftime("%Y年%m月%d日")
        df = self._wc.get(
            query=f"{d}涨停的股票，首次涨停时间，封单金额，成交额，流通市值，换手率，炸板次数，所属行业",
            loop=True,
        )
        if df is None or df.empty:
            return []
        out: list[ZTStock] = []
        for _, row in df.iterrows():
            code = normalize_code(str(_pick(row, "代码")))
            name = str(_pick(row, "简称", "名称"))
            first_zt = str(_pick(row, "首次涨停时间", "涨停时间", default=""))[-8:]
            zhaban = int(_to_float(_pick(row, "炸板次数", default=0)))
            out.append(ZTStock(
                code=code, name=name, trade_date=trade_date,
                zt_time=first_zt,
                fengdan_amt=_to_float(_pick(row, "封单金额", default=0)),
                amount=_to_float(_pick(row, "成交额", default=0)),
                float_mv=_to_float(_pick(row, "流通市值", default=0)),
                turnover=_to_float(_pick(row, "换手率", default=0)),
                zhaban_count=zhaban,
                industry=str(_pick(row, "所属行业", "行业", default="")),
                is_st="ST" in name.upper() or "退" in name,
                is_yizi=bool(first_zt) and first_zt <= "09:30:00" and zhaban == 0,
                is_bj=is_bj_code(code),
            ))
        log.info("[wencai] %s 涨停池 %d 只", trade_date, len(out))
        return out

    def get_zt_pool_zhaban(self, trade_date: date) -> list[ZTStock]:
        d = trade_date.strftime("%Y年%m月%d日")
        df = self._wc.get(query=f"{d}炸板的股票，成交额，流通市值，换手率", loop=True)
        if df is None or df.empty:
            return []
        out = []
        for _, row in df.iterrows():
            code = normalize_code(str(_pick(row, "代码")))
            name = str(_pick(row, "简称", "名称"))
            out.append(ZTStock(
                code=code, name=name, trade_date=trade_date,
                amount=_to_float(_pick(row, "成交额", default=0)),
                float_mv=_to_float(_pick(row, "流通市值", default=0)),
                turnover=_to_float(_pick(row, "换手率", default=0)),
                zhaban_count=1,
                is_st="ST" in name.upper() or "退" in name,
                is_bj=is_bj_code(code),
            ))
        return out

    def get_concept_blocks(self) -> list[ConceptBlock]:
        raise NotImplementedError(
            "pywencai 拉全量概念成分会触发高频风控，概念板块请走 iFinD 或 akshare"
        )
