"""同花顺 iFinD 官方数据接口（主数据源）

依赖: iFinDPy（同花顺官网 quantapi.10jqka.com.cn 下载 DataInterface 包安装）
登录: 环境变量 THS_IFIND_USER / THS_IFIND_PASS（首次运行交互输入后写入用户环境变量）

数据获取策略:
  - 涨停池：THS_WC 问财语句（不耗流量额度），返回字段名做容错匹配
  - 概念板块：THS_DP 数据池（同花顺概念板块成分）
"""
from __future__ import annotations

import os
from datetime import date

from src.datasources.base import (
    ConceptBlock, DataSource, ZTStock, is_bj_code, normalize_code,
)
from src.log import get_logger

log = get_logger(__name__)

_YIZI_TIME_THRESHOLD = "09:30:00"


def _pick(row, *candidates, default=""):
    """从 DataFrame 行里按候选列名容错取值（问财返回列名会变）"""
    for c in candidates:
        for col in row.index:
            if c in str(col):
                v = row[col]
                return default if v is None else v
    return default


def _to_float(v) -> float:
    try:
        s = str(v).replace(",", "").replace("亿", "e8").replace("万", "e4")
        return float(eval(s)) if "e" in s.lower() else float(s)
    except (ValueError, TypeError):
        return 0.0


class IFindSource(DataSource):
    name = "ifind"

    def __init__(self):
        try:
            from iFinDPy import THS_iFinDLogin, THS_WC, THS_DP  # noqa: F401
            self._login_fn = THS_iFinDLogin
            self._wc = THS_WC
            self._dp = THS_DP
        except ImportError as e:
            raise RuntimeError(
                "iFinDPy 未安装。请到 quantapi.10jqka.com.cn 下载同花顺数据接口包"
            ) from e
        self._logged_in = False

    def _ensure_login(self) -> None:
        if self._logged_in:
            return
        user = os.environ.get("THS_IFIND_USER", "")
        passwd = os.environ.get("THS_IFIND_PASS", "")
        if not user or not passwd:
            raise RuntimeError(
                "缺少 iFinD 账号：请设置环境变量 THS_IFIND_USER / THS_IFIND_PASS"
            )
        ret = self._login_fn(user, passwd)
        if ret not in (0, -201):  # -201 = 已登录
            raise RuntimeError(f"iFinD 登录失败, 返回码: {ret}")
        self._logged_in = True
        log.info("iFinD 登录成功")

    def is_available(self) -> bool:
        try:
            self._ensure_login()
            r = self._wc("上证指数", "stock")
            return getattr(r, "errorcode", -1) == 0
        except Exception as e:
            log.warning("iFinD 探活失败: %s", e)
            return False

    # ------------------------------------------------ 涨停池
    def get_zt_pool(self, trade_date: date) -> list[ZTStock]:
        self._ensure_login()
        d = trade_date.strftime("%Y-%m-%d")
        r = self._wc(
            f"{d}涨停；首次涨停时间；封单金额；成交额；流通市值；换手率；"
            f"炸板次数；所属同花顺行业",
            "stock",
        )
        if getattr(r, "errorcode", -1) != 0:
            raise RuntimeError(f"问财涨停池查询失败: {getattr(r, 'errmsg', '')}")
        df = r.data
        if df is None or df.empty:
            log.info("[ifind] %s 涨停池为空", trade_date)
            return []
        out: list[ZTStock] = []
        for _, row in df.iterrows():
            code = normalize_code(str(_pick(row, "代码", "thscode")))
            name = str(_pick(row, "名称", "股票简称"))
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
                industry=str(_pick(row, "所属同花顺行业", "行业", default="")),
                is_st="ST" in name.upper() or "退" in name,
                is_yizi=bool(first_zt) and first_zt <= _YIZI_TIME_THRESHOLD and zhaban == 0,
                is_bj=is_bj_code(code),
                lianban_hint=int(_to_float(_pick(row, "连续涨停天数", "连板", default=0))),
            ))
        log.info("[ifind] %s 涨停池 %d 只", trade_date, len(out))
        return out

    def get_zt_pool_zhaban(self, trade_date: date) -> list[ZTStock]:
        self._ensure_login()
        d = trade_date.strftime("%Y-%m-%d")
        r = self._wc(f"{d}炸板；首次涨停时间；成交额；流通市值；换手率", "stock")
        if getattr(r, "errorcode", -1) != 0 or r.data is None:
            return []
        out: list[ZTStock] = []
        for _, row in r.data.iterrows():
            code = normalize_code(str(_pick(row, "代码", "thscode")))
            name = str(_pick(row, "名称", "股票简称"))
            out.append(ZTStock(
                code=code, name=name, trade_date=trade_date,
                zt_time=str(_pick(row, "首次涨停时间", "涨停时间", default=""))[-8:],
                amount=_to_float(_pick(row, "成交额", default=0)),
                float_mv=_to_float(_pick(row, "流通市值", default=0)),
                turnover=_to_float(_pick(row, "换手率", default=0)),
                zhaban_count=1,
                is_st="ST" in name.upper() or "退" in name,
                is_bj=is_bj_code(code),
            ))
        return out

    # ------------------------------------------------ 概念板块
    def get_concept_blocks(self) -> list[ConceptBlock]:
        """iFinD 数据池拉同花顺概念板块成分。板块列表随版本不同，
        此处先取概念板块列表数据池，再逐个拉成分。"""
        self._ensure_login()
        d = date.today().strftime("%Y-%m-%d")
        # 同花顺概念板块数据池 ID（板块成分函数 block 模式）
        blocks = self._dp("block", f"{d};001016010", "date:Y,thscode:Y,security_name:Y")
        if getattr(blocks, "errorcode", -1) != 0:
            raise RuntimeError(f"概念板块列表拉取失败: {getattr(blocks, 'errmsg', '')}")
        out: list[ConceptBlock] = []
        # 数据池返回板块内成分；板块级枚举需用 THS_HF/行业函数，
        # 免费额度下以问财查询概念列表为补充路径
        r = self._wc("同花顺概念板块列表", "zhishu")
        if getattr(r, "errorcode", -1) == 0 and r.data is not None:
            for _, row in r.data.iterrows():
                name = str(_pick(row, "名称", "板块"))
                cons = self._wc(f"属于{name}的股票", "stock")
                if getattr(cons, "errorcode", -1) == 0 and cons.data is not None:
                    codes = [
                        normalize_code(str(_pick(c, "代码", "thscode")))
                        for _, c in cons.data.iterrows()
                    ]
                    out.append(ConceptBlock(name=name, codes=codes, source=self.name))
        log.info("[ifind] 概念板块 %d 个", len(out))
        return out
