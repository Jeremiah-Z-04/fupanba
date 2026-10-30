"""题材归并：把成员高度重叠的同主线小题材合并成大主线。

开盘啦口径：题材按"主线"聚合（如 机器人 大板块包含 人形机器人/减速器/伺服电机 等细分）。
数据驱动做法：计算题材两两成员 Jaccard 重叠率，超过阈值用 Union-Find 归并成一个主线。

命名：归并组内题材名的「最长公共子串」作为主线词（如 人形机器人+机器人概念 -> 机器人）；
无公共词时取成员最多的题材名。
"""
from __future__ import annotations

from src.log import get_logger

log = get_logger(__name__)


def _lcs(a: str, b: str) -> str:
    """最长公共子串（非子序列），用于提取中文题材名的公共核心词。"""
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    best, end = 0, 0
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
                if dp[i][j] > best:
                    best, end = dp[i][j], i
    return a[end - best:end]


def _pick_name(names: list[str], clusters: dict) -> str:
    """取成员最多的题材名作主线名。

    不重新造词：早期用「最长公共子串」命名，会把「三季报增长」改成「三季报」，
    合并进来一个词反而让板块丢了含义。成员最多者天然是主线，也最可预期。
    """
    if len(names) == 1:
        return names[0]
    return max(names, key=lambda m: len(clusters[m]))


def merge_themes(clusters: dict[str, list[dict]], threshold: float = 0.4) -> dict[str, list[dict]]:
    """成员 Jaccard 重叠率 >= threshold 的题材归并成一个主线。

    额外要求：两个题材名必须有 >=2 字的公共子串（如「三季报落地」+「三季报增长」）。
    多归属（一股进多个板块）会让不相干板块的成员高度重叠（Jaccard 甚至为 1.0），
    只按成员归并会把「AI应用 + 华为概念 + 元宇宙 + 光伏」串成一个大杂烩。
    """
    names = list(clusters.keys())
    if len(names) <= 1:
        return clusters

    code_sets = {n: {s["code"] for s in m} for n, m in clusters.items()}
    parent = {n: n for n in names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            if len(_lcs(a, b)) < 2:      # 名称毫无关系，成员再像也不合并
                continue
            inter = len(code_sets[a] & code_sets[b])
            # 重叠系数（交集 / 较小集）：1 只的「三季报落地」被 36 只的
            # 「三季报增长」完全包含时应当合并，用 Jaccard 会因分母过大永远不达标
            small = min(len(code_sets[a]), len(code_sets[b]))
            if small and inter / small >= threshold:
                parent[find(b)] = find(a)

    groups: dict[str, list[str]] = {}
    for n in names:
        groups.setdefault(find(n), []).append(n)

    merged: dict[str, list[dict]] = {}
    for group in groups.values():
        if len(group) > 1:
            log.info("题材归并: %s -> 主线", " + ".join(group))
        main = _pick_name(group, clusters)
        seen: dict[str, dict] = {}
        for m in group:
            for s in clusters[m]:
                seen.setdefault(s["code"], s)
        merged[main] = list(seen.values())

    return dict(sorted(merged.items(), key=lambda kv: -len(kv[1])))
