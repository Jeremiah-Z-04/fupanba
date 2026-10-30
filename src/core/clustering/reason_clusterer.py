"""涨停原因驱动的动态题材聚类（MVP2 规则聚类）

核心：同花顺涨停池自带 limit_up_reason（涨停原因），如
"人形机器人+橡胶骨架材料+全球销售"，这是市场资金当天对个股的题材定位。
用它做题材归堆，比静态概念成分（撒网式，一只股挂 8 个标签）更贴近资金逻辑。

规则：
  1. reason 按 +/，/,/、 分割成关键词，每个关键词是一个题材线索
  2. 停用词过滤明显噪音词（config: clustering.reason_stopwords，如"业绩增长"）
  3. 主线归并：细分词按 config: clustering.theme_families 归到主线
     （人形机器人/机器人零部件/具身智能/物理AI -> 机器人），避免同一条主线被
     拆成十几个 2~3 只的小簇而通不过 min_size
  4. 同一主线内涨停股 >= min_size → 成立题材
  5. 概念补全（成员制）：已成立主线 + 其核心概念（与主线同名、成分不过宽）→
     该概念下的池内个股全部纳入该主线，**允许一股多题材**。
     涨停原因是当日资金逻辑，概念是静态归属，两者互补才能覆盖"原因没写但确实是
     这个板块"的个股（如创/科高涨幅股没有涨停原因）。
  6. 宽泛概念黑名单 + 成分数量上限（concept_max_members）双重拦截大杂烩概念。
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from src.config import Config
from src.core.clustering.base import Clusterer
from src.log import get_logger
from src.paths import is_frozen
from src.storage.snapshot_repo import SnapshotRepo

log = get_logger(__name__)

_SPLIT_RE = re.compile(r"[+，,、/;；]")

# 同花顺概念名后缀剥离：「PCB概念」->「PCB」，与题材锚点（裸词）对齐
_CONCEPT_SUFFIX_RE = re.compile(r"(概念|板块|指数|题材|产业链)$")

# 开盘啦题材名后缀（「机器人概念」->「机器人」、「冰雪产业」->「冰雪」），
# 剥掉后参与原因词的包含匹配：「机器人」⊂「人形机器人」才能归进机器人板块
_THEME_SUFFIX_RE = re.compile(r"(概念|产业|板块|指数|题材|产业链)$")

# ---------------------------------------------------------------- 业绩语义归一
# 同花顺的涨停原因对「业绩好」有十几种写法：三季报增长/三季报大增/三季报高增/
# 三季报净利增长/三季度净利增/第三季度业绩高增……字面互不包含，靠词表穷举
# 必然漏（实测 10-30 被拆成 9 个题材）。这里按「报告期 + 增长语义」归一，
# 报告期之间**不合并**（用户口径：中报/年报是不同炒作逻辑，不能并进三季报）。
_PERIOD_ALIASES: list[tuple[str, tuple[str, ...]]] = [
    ("一季报", ("一季报", "第一季度", "一季度")),
    ("中报", ("中报", "半年报")),
    ("三季报", ("三季报", "第三季度", "三季度")),
    ("年报", ("年报", "年度", "全年")),
]
_GROWTH_WORDS = ("增长", "大增", "高增", "预增", "净利增", "暴增", "扭亏",
                 "环增", "业绩增", "翻倍", "创新高")
# 亏损/下滑语义不能进「增长」题材（「三季报预亏」是另一条线）
_DECLINE_WORDS = ("亏损", "预亏", "下滑", "下降", "减少", "降低", "减退", "暴雷")


def _performance_theme(word: str) -> str | None:
    """业绩类关键词 -> 「X季报增长」。非业绩词返回 None。

    只做同一报告期内的**说法归一**，不跨报告期合并：
        三季报大增 / 三季报高增 / 三季报净利增长 / 三季度净利增 / 三季报
            -> 三季报增长
        中报预增 -> 中报增长（与三季报分开）
    """
    if not word or any(w in word for w in _DECLINE_WORDS):
        return None
    period = None
    for std, aliases in _PERIOD_ALIASES:
        if any(a in word for a in aliases):
            period = std
            break
    if not period:
        return None
    # 纯报告期词（「三季报」单独出现）也算业绩线
    if word.strip() in {a for _, aliases in _PERIOD_ALIASES for a in aliases}:
        return f"{period}增长"
    if any(w in word for w in _GROWTH_WORDS):
        return f"{period}增长"
    # 报告期后只跟一个「增」字（「三季报增」）；排除增持/增发/增资/减持这类非业绩词
    if "增" in word and not any(x in word for x in ("增持", "增发", "增资", "减持")):
        return f"{period}增长"
    return None


class ReasonClusterer(Clusterer):
    name = "reason"

    def __init__(self, cfg: Config, repo: SnapshotRepo):
        self.min_size = cfg.get("clustering.min_cluster_size", 2)
        self.repo = repo
        self.stopwords = set(cfg.get("clustering.reason_stopwords", []) or [])
        self.keep_single = [str(k) for k in (cfg.get("clustering.keep_single", []) or [])]
        self.merge_threshold = cfg.get("clustering.merge_threshold", 0.4)
        self.concept_max_members = cfg.get("clustering.concept_max_members", 600)
        self.blacklist = set(cfg.get("clustering.generic_concept_blacklist", []) or [])
        families = cfg.get("clustering.theme_families", {}) or {}
        # 打包版（发给朋友的 exe）优先用同步下来的归并规则：
        # theme_families 活在配置文件里，不跟着框架同步就会和你这边结果不一致
        if is_frozen():
            from src.core.framework_sync import read_meta_json
            synced = read_meta_json(self.repo.db, "kpl_families")
            if isinstance(synced, dict) and synced:
                families = synced
        # 长词优先匹配，避免"机器人"抢先吃掉"机器人零部件"
        self._patterns: list[tuple[str, str]] = []
        if isinstance(families, dict):
            for family, pats in families.items():
                for pat in (pats or []):
                    if pat:
                        self._patterns.append((str(pat), str(family)))
        self._patterns.sort(key=lambda x: -len(x[0]))
        # 主线 -> 允许做成员补全的核心概念（不配则用同名概念兜底）
        self.core_concepts: dict[str, list[str]] = cfg.get(
            "clustering.theme_core_concepts", {}) or {}
        self._core_map = {
            str(c): str(theme)
            for theme, concepts in self.core_concepts.items()
            for c in (concepts or [])
        }
        # 概念名 -> 主线名。板块改名/合并后，同花顺概念还挂着老名字，
        # 不接一下就会独立成一个「1 只」的孤儿板块（比如大消费合并后仍冒出「食品饮料」）
        alias_cfg = cfg.get("clustering.concept_theme_alias", {}) or {}
        if is_frozen():
            from src.core.framework_sync import read_meta_json
            synced = read_meta_json(self.repo.db, "kpl_alias")
            if isinstance(synced, dict) and synced:
                alias_cfg = synced
        self.concept_alias: dict[str, str] = {
            str(k): str(v) for k, v in alias_cfg.items()
        }
        self._sizes: dict[str, int] | None = None

        # 开盘啦题材层级表（细分词 -> 一级题材），把市场口语词归到官方大板块
        self.use_kpl = bool(cfg.get("clustering.use_kpl_framework", False))
        self._kpl_exact: dict[str, str] = {}        # 词 -> 一级题材
        self._kpl_include: list[tuple[str, str]] = []  # [(匹配词, 一级题材)] 长词在前
        self._kpl_themes: dict[str, list[str]] = {}    # 一级题材 -> 细分词
        if self.use_kpl:
            self._load_kpl(cfg)

    def _load_kpl(self, cfg: Config) -> None:
        """加载题材框架：以数据库 kpl_theme 表为准，空库时用 yaml 做首次种子导入。

        数据库优先是为了支持网页端人工增删改（yaml 整文件重写会丢失手工修改）。
        yaml 保留作为种子/备份：库被清空时自动重新导入。
        """
        themes = self._load_themes_from_db(cfg)
        if themes is None:
            themes = self._load_themes_from_yaml(cfg)
        if not themes:
            log.warning("题材框架为空，退回纯关键词聚类")
            self.use_kpl = False
            return
        self._kpl_themes = themes

        # 2 字词默认不做包含匹配（「汽车」会吞掉「汽车芯片」），
        # 只放行白名单里那些本身就是独立主线的 2 字题材词；
        # 人工在面板上配过的词一律放行（人已经确认过归属，可信度够）
        two_char_ok = {str(w) for w in (cfg.get(
            "clustering.kpl_two_char_include", []) or [])}
        try:
            manual = {
                str(r["sub_name"]) for r in self.repo.db.query(
                    "SELECT sub_name FROM kpl_theme "
                    "WHERE source='manual' AND enabled=1 AND sub_name<>''")
            }
            two_char_ok |= {w for w in manual if len(w) == 2}
        except Exception:
            pass
        include: dict[str, tuple[str, str]] = {}
        for theme, subs in self._kpl_themes.items():
            # 题材名本身：命中后只归一级，二级留空
            self._kpl_exact.setdefault(theme, (theme, ""))
            bare_theme = _THEME_SUFFIX_RE.sub("", theme) or theme
            if bare_theme != theme:
                self._kpl_exact.setdefault(bare_theme, (theme, ""))
            for cand in {theme, bare_theme}:
                if len(cand) >= 3 or cand in two_char_ok:
                    include.setdefault(cand, (theme, ""))
            # 细分词：命中后二级标出标准细分名（原因里写「冰雪旅游保障」，
            # 命中锚点「冰雪」，二级仍显示框架里的标准词「冰雪产业」）
            for sub in subs:
                self._kpl_exact.setdefault(sub, (theme, sub))
                bare_sub = _THEME_SUFFIX_RE.sub("", sub) or sub
                if bare_sub != sub:
                    self._kpl_exact.setdefault(bare_sub, (theme, sub))
                for cand in {sub, bare_sub}:
                    if len(cand) >= 3 or cand in two_char_ok:
                        include.setdefault(cand, (theme, sub))
        # 长词优先：命中「半导体设备」比命中「半导体」更具体
        self._kpl_include = sorted(include.items(), key=lambda kv: -len(kv[0]))
        log.info("题材框架已加载: %d 个一级题材, %d 个锚点词（含细分归属）",
                 len(self._kpl_themes), len(self._kpl_include))

    def _load_themes_from_db(self, cfg: Config) -> dict[str, list[str]] | None:
        """读数据库；空库时从 yaml 种子导入。失败返回 None（交给 yaml 兜底）。"""
        try:
            from src.storage.theme_repo import ThemeRepo

            repo = ThemeRepo(self.repo.db)
            themes = repo.tree()
            if themes:
                return themes
            rel = cfg.get("clustering.kpl_framework_path", "config/kpl_framework.yaml")
            from src.paths import APP_DIR, BUNDLE_DIR

            for base in (APP_DIR, BUNDLE_DIR):
                path = Path(base) / rel
                if path.exists():
                    n = repo.seed_if_empty(path)
                    log.info("题材框架首次种子导入: %d 条（来源 %s）", n, path.name)
                    break
            return repo.tree() or None
        except Exception as e:
            log.warning("题材框架读取数据库失败（%s），改用 yaml 只读模式", e)
            return None

    def _load_themes_from_yaml(self, cfg: Config) -> dict[str, list[str]]:
        """兜底：直接从 yaml 读（数据库不可用时保证复盘不中断）。"""
        from src.paths import APP_DIR, BUNDLE_DIR

        rel = cfg.get("clustering.kpl_framework_path", "config/kpl_framework.yaml")
        for base in (APP_DIR, BUNDLE_DIR):
            path = Path(base) / rel
            if not path.exists():
                continue
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception as e:
                log.warning("yaml 框架读取失败（%s）", e)
                return {}
            themes = data.get("themes") or {}
            if isinstance(themes, dict):
                return {str(k): [str(x) for x in (v or [])] for k, v in themes.items()}
        return {}

    def cluster(self, stocks: list[dict], zhaban: list[dict]) -> dict[str, list[dict]]:
        clusters: dict[str, list[dict]] = {}
        seen: dict[str, set[str]] = {}

        # 1. 主路径：涨停原因关键词归堆（主线归并后）
        #    一个关键词可归多个大板块：「冰雪旅游保障」同时含「冰雪」和「旅游」，
        #    既要进体育产业（冰雪产业）也要进旅游，一股多归属是用户口径。
        #    同时记录命中的二级细分词（sub_tags），供题材内分组展示。
        unmatched: dict[str, int] = {}
        loose: dict[str, list[str]] = {}     # 未命中词 -> 用它成块的股票代码
        framed: set[str] = set()             # 至少有一个原因词命中框架的股票
        for s in stocks:
            for kw in self._extract(s.get("reason", "")):
                pairs = self._canonicals_detail(kw)
                is_loose = (len(pairs) == 1 and pairs[0] == (kw, "")
                            and kw not in self._kpl_exact)
                if is_loose:
                    unmatched[kw] = unmatched.get(kw, 0) + 1
                    loose.setdefault(kw, []).append(s["code"])
                else:
                    framed.add(s["code"])
                for theme, sub in pairs:
                    # 二级标签：优先用框架里的标准细分词；没命中细分时用原因原文
                    # 兜底（如「焊接机器人」-> 机器人/焊接机器人），便于复盘查看
                    tag = sub
                    if not tag and kw != theme and len(kw) <= 12:
                        tag = kw
                    bucket = clusters.setdefault(theme, [])
                    codes = seen.setdefault(theme, set())
                    if s["code"] in codes:
                        # 已在该题材下：补二级细分（一股可在同一板块占多个细分）
                        if tag:
                            for m in bucket:
                                if m["code"] == s["code"]:
                                    cur = [x for x in (m.get("sub_tags") or "").split(",") if x]
                                    if tag not in cur:
                                        m["sub_tags"] = ",".join([*cur, tag])
                                    break
                        continue
                    # 浅拷贝：同一只股在不同题材下需要各自独立的 sub_tags
                    row = dict(s)
                    row["sub_tags"] = tag
                    bucket.append(row)
                    codes.add(s["code"])
        if unmatched:
            top = sorted(unmatched.items(), key=lambda kv: -kv[1])[:15]
            log.info("框架未命中词 %d 个（独立成题材）: %s", len(unmatched),
                     "、".join(f"{w}({n})" for w, n in top))

        # 先按 min_size 卡掉纯噪音锚点，再做概念补全。
        # 顺序不能反：否则一个只有 1~2 只的噪音词（如"AI应用"）会把几十只
        # 只有该概念、没有涨停原因的创/科异动股全吸进来，凭空造出一个大题材。
        #
        # 孤词更严格：**不在框架里的词至少 2 只才成板块**。
        # 一只股的原因通常写 3~5 个词，命中框架的那个已经让它进了大板块，
        # 剩下的「焊接切割设备」「北美订单」「超跌反弹」再各自成块，题材就会
        # 从 16 个膨胀到 78 个（2026-10-30 实测）。框架内的题材不受此限，
        # 单只的冷门主线（如某天只有一只冰雪产业）照样显示。
        before = len(clusters)
        family_names = {f for _, f in self._patterns}
        anchors = {
            k: v for k, v in clusters.items()
            if (k in self._kpl_themes or k in family_names
                or len(v) >= 2 or self._is_kept(k))
        }
        clusters = anchors

        # 2. 概念补全（成员制）：只补「涨停原因为空」的股票（创/科异动股、涨停但
        #    原因延迟的股）。有涨停原因的股票以 reason 为准，不参与概念补全，
        #    否则静态概念（如「低空经济」）会把 reason 没写该题材的涨停股拖进来。
        #    只认「核心概念」（与主线同名、且成分不过宽），宽泛概念一律不认。
        sizes = self._concept_sizes()
        for s in stocks:
            if s.get("reason"):
                continue
            for c in self.repo.concepts_of(s["code"]):
                key = self._concept_theme(c)
                if not key or key not in clusters:
                    continue
                if self._is_broad(c, sizes):
                    continue
                codes = seen.setdefault(key, set())
                if s["code"] in codes:
                    continue
                row = dict(s)
                row["sub_tags"] = ""   # 概念补进来的没有细分依据
                clusters[key].append(row)
                codes.add(s["code"])

        # 3. 概念反查兜底：**涨停原因里所有词都没命中框架**的股票（完全裸奔，
        #    如原因只写「中式面点+小笼包」），用同花顺概念反查框架细分词归到
        #    大板块。只处理完全无归属的股——只要有一个原因词命中了框架，就以
        #    原因词为准，不掺概念（否则「上海沪工」会因为挂着「新能源汽车」
        #    概念被塞进汽车板块，当天炒的根本不是这个）。
        #    不限制归属个数（用户口径：宁可多归属，也不要散落成孤词）。
        by_code = {s["code"]: s for s in stocks}
        rescued: dict[str, list[str]] = {}
        for kw, codes in loose.items():
            moved: list[str] = []
            for code in codes:
                if code in framed:
                    continue
                s = by_code.get(code)
                if not s:
                    continue
                for c in self.repo.concepts_of(code):
                    if self._is_broad(c, sizes):
                        continue
                    hit = self._concept_subject(c)
                    if not hit:
                        continue
                    theme, sub = hit
                    bucket = clusters.setdefault(theme, [])
                    cs = seen.setdefault(theme, set())
                    if code in cs:
                        continue
                    row = dict(s)
                    row["sub_tags"] = sub
                    bucket.append(row)
                    cs.add(code)
                    moved.append(code)
                    rescued.setdefault(s["name"] or code, []).append(f"{theme}/{sub}")
            if moved:
                left = [m for m in clusters.get(kw, []) if m["code"] not in set(moved)]
                if left:
                    clusters[kw] = left
                else:
                    clusters.pop(kw, None)
                    seen.pop(kw, None)
        if rescued:
            log.info("概念兜底: %d 只无归属个股按同花顺概念归入大板块（示例 %s）",
                     len(rescued),
                     "；".join(f"{k}→{'、'.join(v[:3])}" for k, v in list(rescued.items())[:3]))

        # 4. 题材归并：成员高度重叠的同主线小题材合并成大主线
        if self.merge_threshold > 0:
            from src.core.clustering.merge import merge_themes
            clusters = merge_themes(clusters, self.merge_threshold)

        log.info(
            "涨停原因聚类: %d 只涨停 -> %d 个题材锚点（>=%d 只 %d 个），补全+归并后 %d 个题材",
            len(stocks), before, self.min_size, len(anchors), len(clusters),
        )
        return dict(sorted(clusters.items(), key=lambda kv: -len(kv[1])))

    # ------------------------------------------------ 内部
    def _is_kept(self, key: str) -> bool:
        """白名单题材（如生肖"羊字辈"）即使单股也保留。去掉引号后包含匹配。"""
        norm = key.replace("“", "").replace("”", "").replace('"', "").replace("'", "")
        return any(kw in norm for kw in self.keep_single)

    def _canonical(self, word: str) -> str:
        """单词归一（概念补全等单值场景用）：取 _canonicals 的第一个。"""
        keys = self._canonicals(word)
        return keys[0] if keys else word

    def _canonicals(self, word: str) -> list[str]:
        """只要一级题材名（多归属）。"""
        return [theme for theme, _ in self._canonicals_detail(word)] or [word]

    def _canonicals_detail(self, word: str) -> list[tuple[str, str]]:
        """细分词归到 (一级题材, 二级细分词)，返回**所有**命中。

        优先级：
          1. 用户手工配的 theme_families —— 命中即单归属，二级留空
          2. 业绩类语义归一：报告期 + 增长语义 -> 「X季报增长」，二级留空
          3. 开盘啦层级表：精确命中 → 只归该题材；否则返回全部包含命中
             （「冰雪旅游保障」→ [(旅游,''), (体育产业,'冰雪产业')]）
          4. 都没命中 → 保留原词独立成题材，二级留空
        """
        for pat, family in self._patterns:
            if pat in word:
                return [(family, "")]
        perf = _performance_theme(word)
        if perf:
            return [(perf, "")]
        if not self.use_kpl:
            return [(word, "")]
        hit = self._kpl_exact.get(word)
        if hit:
            return [hit]
        out: list[tuple[str, str]] = []
        for cand, pair in self._kpl_include:
            if cand in word and pair not in out:
                out.append(pair)
        return out or [(word, "")]

    def _concept_subject(self, concept: str) -> tuple[str, str] | None:
        """概念名 -> 框架里的 (大板块, 细分词)。

        只认框架里**确实存在的细分词**（461 个），不认一级题材名的松散包含，
        避免「专精特新」这类泛概念把股票塞得到处都是。
        """
        if not self.use_kpl or not concept:
            return None
        for key in (concept, _THEME_SUFFIX_RE.sub("", concept) or concept):
            hit = self._kpl_exact.get(key)
            if hit and hit[1]:      # sub 非空 = 命中的是细分词
                return hit
        return None

    def _concept_theme(self, concept: str) -> str | None:
        """概念 -> 归属主线。

        配了 theme_core_concepts 的主线只认白名单里的概念：机器人只认「人形机器人」，
        不会因为概念叫「减速器」就把整条减速器产业链都算进机器人板块。
        """
        alias = self.concept_alias.get(concept)
        if alias:
            return alias
        theme = self._core_map.get(concept)
        if theme:
            return theme
        key = self._canonical(concept)
        if key in self.core_concepts:
            return None
        # 概念名后缀剥离：「PCB概念」->「PCB」；剥离后撞上核心概念白名单
        # （如「机器人概念」已白名单）的仍按白名单口径处理
        stripped = _CONCEPT_SUFFIX_RE.sub("", key)
        if stripped and stripped != key:
            if stripped in self.core_concepts:
                return None
            key = stripped
        return key

    def _concept_sizes(self) -> dict[str, int]:
        if self._sizes is None:
            try:
                self._sizes = self.repo.concept_sizes()
            except Exception:
                self._sizes = {}
        return self._sizes

    def _is_broad(self, concept: str, sizes: dict[str, int]) -> bool:
        if concept in self.blacklist:
            return True
        if self.concept_max_members and sizes.get(concept, 0) > self.concept_max_members:
            return True
        return False

    def _extract(self, reason: str) -> list[str]:
        if not reason:
            return []
        out: list[str] = []
        for part in _SPLIT_RE.split(reason):
            kw = part.strip()
            if not kw or kw in self.stopwords:
                continue
            if kw not in out:
                out.append(kw)
        return out
