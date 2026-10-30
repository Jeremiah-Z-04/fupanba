"""龙头梯队排序：板幅归一 + 复合次因子模型

优先级（与产品方确认，2026-10-29 调整）:
  1. 第一主因子：板幅归一强度（封板股恒 1.0；创/科未封板异动股 = 涨幅/板幅，
     如 13cm/20cm=0.65，比"谁先到 9.90/板幅"，20cm 涨停股也有一席之地）
  2. 同强度内硬排序：上板时间（越早越强；异动股无上板时间排后）
  3. 连板高度降序（同强度同时间档内，高连板优先）
  4. 以上都相同时，再用加权次因子微调：封单流通占比 > 合理换手 > 流通市值
     （权重在 YAML leader.weights 配置）
输出：簇内排序 + 龙1/龙2/... 标签
"""
from __future__ import annotations

from src.config import Config
from src.log import get_logger

log = get_logger(__name__)


def _time_key(t: str) -> int:
    """'09:25:00' 或 '09:30' -> 秒数，越早越小越前；空值/异常排最后"""
    if not t:
        return 999999
    parts = str(t).split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        s = int(parts[2]) if len(parts) > 2 else 0
        return h * 3600 + m * 60 + s
    except (ValueError, IndexError):
        return 999999


def _limit_pct(code: str) -> int:
    """按板块归属返回涨停板幅：创业板/科创板 20%，北交所 30%，主板 10%"""
    c = str(code or "")
    if c.startswith(("30", "68")):
        return 20
    if c.startswith(("43", "83", "87", "88", "92")):
        return 30
    return 10


def _board_strength(s: dict) -> float:
    """板幅归一强度：封板股恒 1.0；未封板异动股 = 涨幅/板幅（13cm/20cm=0.65）"""
    if s.get("sealed") == 1:
        return 1.0
    pct = float(s.get("high_pct", 0.0) or 0.0)
    return pct / _limit_pct(s.get("code", ""))


class LeaderRanker:
    def __init__(self, cfg: Config):
        leader = cfg.section("leader")
        self.top_n = leader.get("top_n", 3)
        self.weights = leader.get("weights", {})
        self.sweet_spot = leader.get("turnover_sweet_spot", [5, 25])
        self.yizi_bonus = leader.get("yizi_bonus", 0.0)

    def rank(self, members: list[dict]) -> list[dict]:
        """对单个题材簇排序，返回带 leader_rank / leader_label 的列表。

        members 元素需含: code, name, lianban_height, fengdan_amt, float_mv,
                         zt_time, turnover, is_yizi
        """
        if not members:
            return []

        # 同板内次因子归一化（上板时间单独作硬排序键，不参与加权）
        fd = [self._fengdan_ratio(s) for s in members]
        to = [self._turnover_score(s.get("turnover", 0.0)) for s in members]
        mv = [s.get("float_mv", 0.0) for s in members]

        def minmax(vals, reverse=False):
            lo, hi = min(vals), max(vals)
            span = hi - lo
            out = [(v - lo) / span if span > 0 else 0.5 for v in vals]
            return [1 - v for v in out] if reverse else out

        fd_n = minmax(fd)
        to_n = minmax(to)
        mv_n = minmax(mv, reverse=True)   # 市值偏小分越高

        w = self.weights
        scored = []
        for i, s in enumerate(members):
            sub = (
                fd_n[i] * w.get("fengdan_ratio", 1.0)
                + to_n[i] * w.get("turnover", 0.5)
                + mv_n[i] * w.get("float_mv", 0.5)
                + (self.yizi_bonus if s.get("is_yizi") else 0.0)
            )
            scored.append((s, sub))

        # 排序优先级：板幅归一强度降序（封板=1.0 恒最前；异动股按涨幅/板幅）
        #             > 上板时间升序（早上板=辨识度，异动股无时间排后）
        #             > 连板高度降序 > 次因子综合分降序（封单/换手/市值微调）
        scored.sort(key=lambda x: (
            -_board_strength(x[0]),
            _time_key(x[0].get("zt_time", "")),
            -int(x[0].get("lianban_height", 0) or 0),
            -x[1],
        ))

        out = []
        for rank, (s, sub) in enumerate(scored, start=1):
            row = dict(s)
            row["leader_rank"] = rank
            row["leader_label"] = f"龙{rank}" if rank <= self.top_n else ""
            row["leader_score"] = round(sub, 3)
            out.append(row)
        return out

    def pick_leaders(self, members: list[dict]) -> list[dict]:
        """只保留前 N 个龙标"""
        return [s for s in self.rank(members) if s["leader_rank"] <= self.top_n]

    def _fengdan_ratio(self, s: dict) -> float:
        mv = s.get("float_mv", 0.0)
        return s.get("fengdan_amt", 0.0) / mv * 100 if mv > 0 else 0.0

    def _turnover_score(self, t: float) -> float:
        """合理换手：落在 sweet_spot 区间得满分，偏离线性衰减"""
        lo, hi = self.sweet_spot
        if lo <= t <= hi:
            return 1.0
        if t <= 0:
            return 0.0
        if t < lo:
            return t / lo
        return max(0.0, 1.0 - (t - hi) / hi)
