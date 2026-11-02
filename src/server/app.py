"""FastAPI 后端：网页操作台的全部 API

与 CLI 共用 Pipeline，行为一致。只监听 127.0.0.1，不暴露局域网。
"""
from __future__ import annotations

import base64
import hmac
import re
import os
import threading
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.config import Config
from src.core.trading_calendar import get_calendar
from src.core.updater import apply_update, check_for_update
from src.datasources import fuyao_api as fy
from src.export import ths_files as ths
from src.export.clipboard import (ClipboardUnavailable, build_code_list,
                                  build_code_groups, copy_to_clipboard)
from src.log import get_logger
from src.paths import BUNDLE_DIR, is_frozen
from src.pipeline import Pipeline
from src.storage.note_repo import NoteRepo
from src.storage.override_repo import OverrideRepo

log = get_logger(__name__)
WEB_DIR = BUNDLE_DIR / "web"


# ---------------- 请求模型 ----------------
class DateReq(BaseModel):
    date: str | None = None
    theme: str | None = None          # 仅剪贴板用：指定题材则只导该题材
    group_index: int | None = None    # 仅剪贴板用：分次复制时指定第几组（0 起）

class BackfillReq(BaseModel):
    days: int = 30

class VolBackfillReq(BaseModel):
    days: int = 20
    force: bool = False

class NoteReq(BaseModel):
    date: str | None = None
    scope: str = "day"        # day | theme | stock
    key: str = ""
    content: str = ""

class RerankReq(BaseModel):
    date: str
    theme: str
    code: str
    new_rank: int

class MoveOutReq(BaseModel):
    date: str
    theme: str
    code: str
    reason: str = ""

class AddToThemeReq(BaseModel):
    date: str
    theme: str
    code: str

class ResetThemeReq(BaseModel):
    date: str
    theme: str

class ExportThsReq(BaseModel):
    date: str | None = None
    theme: str | None = None          # scope=single 时必填
    scope: str = "single"             # single | all
    include_single: bool = False      # all 模式下是否保留只含 1 只的题材

class ImageItem(BaseModel):
    path: str
    data_b64: str

class ImageUploadReq(BaseModel):
    items: list[ImageItem]

class RevealReq(BaseModel):
    path: str

class OpenThsReq(BaseModel):
    date: str | None = None


# ---------------- 题材框架（kpl_theme） ----------------
class ThemeAddReq(BaseModel):
    theme_name: str
    sub_name: str = ""
    note: str = ""


class ThemeUpdateReq(BaseModel):
    theme_name: str
    sub_name: str = ""
    new_theme: str | None = None
    new_sub: str | None = None
    enabled: int | None = None
    note: str | None = None


class ThemeKeyReq(BaseModel):
    theme_name: str
    sub_name: str = ""


class ThemeImportReq(BaseModel):
    path: str | None = None


class FwApplyReq(BaseModel):
    date: str | None = None


def _raw(s: str | None) -> date:
    """请求里的日期原样解析（不修正休市）。"""
    return date.fromisoformat(s) if s else date.today()


_SPLIT_RE = re.compile(r"[+，,、/;；]")


def _reason_keywords(reason: str, stopwords: set[str]) -> list[str]:
    """涨停原因 -> 关键词列表（与聚类同一套切词规则 + 停用词）"""
    out: list[str] = []
    for part in _SPLIT_RE.split(reason or ""):
        kw = part.strip()
        if kw and kw not in stopwords and kw not in out:
            out.append(kw)
    return out


def _common_keywords(members: list[dict], stopwords: set[str], top: int = 5) -> list[dict]:
    """同题材的共性关键词：统计整个题材里出现 >=2 次的涨停原因词。

    这是「算法归纳」而不是新闻催化——用来替代同花顺悬浮卡里的「行业共性」，
    数据全部来自当日涨停原因，可验证、不编造。
    """
    counter: dict[str, int] = {}
    for m in members:
        for kw in _reason_keywords(m.get("reason") or "", stopwords):
            counter[kw] = counter.get(kw, 0) + 1
    items = [(k, n) for k, n in counter.items() if n >= 2]
    items.sort(key=lambda kv: (-kv[1], -len(kv[0])))
    return [{"kw": k, "n": n} for k, n in items[:top]]


def create_app() -> FastAPI:
    cfg = Config.load()
    pipe = Pipeline(cfg)
    ovr = OverrideRepo(pipe.db)
    notes = NoteRepo(pipe.db)
    cal = get_calendar(cfg.data_dir)
    app = FastAPI(title="ThemeHunter", version="0.1.0")

    # 静态资源禁缓存：版本号只写在 HTML 里，浏览器一旦缓存旧 app.js 就会与
    # 新 HTML 错配（取不到节点 -> 抛错 -> 后面的按钮绑定全部失效）
    @app.middleware("http")
    async def _no_cache_static(request, call_next):
        resp = await call_next(request)
        if request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp

    # 可选密码保护：设了环境变量 TH_AUTH=用户名:密码 才启用 HTTP Basic 认证。
    # 本地跑默认不设，行为不变；部署到公网/局域网时必须设，否则复盘数据谁都能看。
    _auth = os.environ.get("TH_AUTH", "").strip()

    @app.middleware("http")
    async def _basic_auth(request, call_next):
        if not _auth:
            return await call_next(request)
        hdr = request.headers.get("authorization", "")
        if hdr.startswith("Basic "):
            try:
                got = base64.b64decode(hdr[6:]).decode("utf-8")
                if hmac.compare_digest(got, _auth):
                    return await call_next(request)
            except Exception:
                pass
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="ThemeHunter", charset="UTF-8"'},
            content="需要登录",
        )

    def _d(s: str | None) -> date:
        """读取/写入类接口统一落到「实际交易日」：休市日自动用上一交易日，
        避免前端拿着休市日期查不到任何数据（采集/复盘接口除外，它们要回传休市标记）。"""
        return cal.resolve(_raw(s))["trade_date"]

    # ---------------- 页面 ----------------
    @app.get("/")
    def index():
        # 页面不缓存：否则改了前端仍会加载旧 index.html（静态资源用 ?v= 版本号破缓存）
        return FileResponse(WEB_DIR / "index.html",
                            headers={"Cache-Control": "no-cache, must-revalidate"})

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    # ---------------- 采集与复盘 ----------------
    @app.get("/api/update/check")
    def api_update_check():
        return check_for_update(cfg)

    @app.post("/api/update/apply")
    def api_update_apply():
        res = apply_update(cfg)
        if res.get("ok"):
            # 给 response 一点时间写回，再退出进程让 apply.bat 替换文件
            threading.Timer(1.2, lambda: os._exit(0)).start()
        return res

    @app.get("/api/version")
    def api_version():
        """前端版本号（读 index.html 的 meta），页面据此判断自己是不是旧版。"""
        try:
            html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
            m = re.search(r'name="app-version" content="([^"]+)"', html)
            return {"version": m.group(1) if m else ""}
        except Exception:
            return {"version": ""}

    @app.get("/api/day")
    def api_day(date: str | None = None):
        """日期解析：请求日 → 实际数据日（休市日自动回退到上一交易日）。"""
        d = _raw(date)
        info = cal.resolve(d)
        snap = pipe.db.query(
            "SELECT value FROM meta WHERE key=?", (f"snapshot:{info['trade_date']}",)
        )
        return {
            "requested": info["requested"].isoformat(),
            "trade_date": info["trade_date"].isoformat(),
            "is_trading_day": info["is_trading_day"],
            "is_holiday": info["is_holiday"],
            "holiday_kind": info["holiday_kind"],   # weekend / holiday / ""
            "adjusted": info["adjusted"],
            "label": info["label"],                 # 如 2026-10-24（休市中）
            "has_data": pipe.repo.has_date(info["trade_date"]),
            "is_snapshot": bool(snap),              # 该日数据采自盘中（未收盘，不完整）
        }

    @app.post("/api/fetch")
    def api_fetch(req: DateReq):
        try:
            # 传原始请求日，让 pipeline 自己回退并把「休市 + 实际数据日」回传前端
            r = pipe.fetch(_raw(req.date))
            # 采集完成立即复盘：采集/复盘原本是两个按钮，只点采集会得到空题材页
            # （2026-10-29 用户反馈「盘中没有题材数据」即此原因）。
            # 当日已有人工修正时跳过，避免冲掉手工调整的排名（与 reanalyze 口径一致）。
            d = r.get("trade_date")
            if d:
                n = pipe.db.query(
                    "SELECT COUNT(*) AS c FROM leader_result "
                    "WHERE trade_date=? AND is_override=1", (d,))[0]["c"]
                if n:
                    r["auto_run"] = {"skipped": True,
                                     "reason": f"当日有 {n} 条人工修正，未自动复盘"}
                else:
                    try:
                        rr = pipe.run(date.fromisoformat(d), to_clipboard=False)
                        r["auto_run"] = {
                            "skipped": False,
                            "themes_count": rr.get("themes_count", 0),
                            "exported": rr.get("exported", 0),
                        }
                    except Exception as e:
                        log.exception("采集后自动复盘失败")
                        r["auto_run"] = {"skipped": True,
                                         "reason": "自动复盘失败: " + str(e)}
            return r
        except Exception as e:
            log.exception("采集失败")
            raise HTTPException(500, str(e))

    @app.post("/api/backfill")
    def api_backfill(req: BackfillReq):
        try:
            return pipe.backfill(days=req.days)
        except Exception as e:
            log.exception("历史回填失败")
            raise HTTPException(500, str(e))

    @app.post("/api/run")
    def api_run(req: DateReq):
        try:
            return pipe.run(_raw(req.date), to_clipboard=False)
        except Exception as e:
            log.exception("复盘失败")
            raise HTTPException(500, str(e))

    @app.post("/api/concepts/refresh")
    def api_concepts(force: bool = False):
        try:
            return {"count": pipe.refresh_concepts(force=force)}
        except Exception as e:
            raise HTTPException(500, str(e))

    # ---------------- 题材框架（kpl_theme） ----------------
    _fw_cache: dict = {"at": 0.0, "clusterer": None}

    def _fw_clusterer():
        """归类预览用的聚类器（缓存 5 分钟，框架变更时清空）。"""
        import time
        if _fw_cache["clusterer"] is not None and time.time() - _fw_cache["at"] < 300:
            return _fw_cache["clusterer"]
        from src.core.clustering.reason_clusterer import ReasonClusterer
        c = ReasonClusterer(cfg, pipe.repo)
        _fw_cache["clusterer"] = c
        _fw_cache["at"] = time.time()
        return c

    def _fw_repo():
        from src.storage.theme_repo import ThemeRepo
        return ThemeRepo(pipe.db)

    @app.get("/api/framework/themes")
    def api_fw_themes(q: str = ""):
        """题材列表（按一级题材分组）。q 命中题材名或细分词。"""
        repo = _fw_repo()
        rows = repo.list_themes(q)
        groups: dict[str, dict] = {}
        for r in rows:
            name = r["theme_name"]
            g = groups.setdefault(name, {
                "theme_name": name,
                "source": r["source"],
                "enabled": r["enabled"],
                "note": r["note"],
                "subs": [],
            })
            if r["sub_name"]:
                g["subs"].append({
                    "sub_name": r["sub_name"], "source": r["source"],
                    "enabled": r["enabled"], "note": r["note"],
                })
        return {"stats": repo.stats(), "groups": list(groups.values())}

    @app.get("/api/framework/match")
    def api_fw_match(q: str):
        """归类预览：输入任意词，返回它会归到哪个一级题材。"""
        q = (q or "").strip()
        if not q:
            return {"query": "", "themes": []}
        c = _fw_clusterer()
        return {
            "query": q,
            "themes": c._canonicals(q),
            "exact": q in c._kpl_exact,
            "enabled": c.use_kpl,
        }

    @app.get("/api/framework/loose")
    def api_fw_loose(limit: int = 300):
        """未归类词：历史上出现过、但不在框架里的板块名（孤词块）。

        这些词归不到大板块，是「题材碎」的主要来源。前端提供下拉把词挂到
        某个大板块（写入 kpl_theme 细分词，之后自动生效）。
        """
        fams = set(cfg.section("clustering").get("theme_families", {}) or {})
        rows = pipe.db.query(
            "SELECT t.theme_name AS name, COUNT(*) AS days, "
            "       SUM(t.member_count) AS total "
            "FROM theme_result t "
            "WHERE t.theme_name NOT IN (SELECT theme_name FROM kpl_theme) "
            "GROUP BY t.theme_name ORDER BY days DESC, total DESC LIMIT ?",
            (max(1, min(limit, 2000)),),
        )
        items = [
            {"name": r["name"], "days": r["days"], "total": r["total"] or 0}
            for r in rows
            if r["name"] not in fams          # 用户在 theme_families 里配过的不算孤词
        ]
        return {"count": len(items), "items": items}

    @app.post("/api/framework/sync")
    def _fw_reanalyze() -> dict:
        """框架变了之后，把当天结果按新框架重算一次。

        不重算的话，朋友同步到新框架、看到的还是旧归类，要再手点一次复盘才变。
        有人工修正就跳过——重算会覆盖 is_override 的排名，人手工调过的不该被冲掉。
        """
        d = cal.resolve(date.today())["trade_date"]
        iso = d.isoformat()
        n = pipe.db.query(
            "SELECT COUNT(*) AS c FROM leader_result "
            "WHERE trade_date=? AND is_override=1", (iso,))[0]["c"]
        if n:
            return {"skipped": True,
                    "reason": f"{iso} 有 {n} 条人工修正，未自动重算"}
        try:
            r = pipe.analyze_day(d)
            return {"ok": True, "date": iso,
                    "themes": r.get("themes_count", 0)}
        except Exception as e:
            log.exception("框架同步后重算当天失败")
            return {"ok": False, "reason": str(e)}

    def api_fw_sync():
        """从发布方拉取最新题材框架并整表覆盖。

        为什么单独做：应用更新包会保留 data/（数据库不动），而框架就在库里，
        所以光发新 exe 不会让框架跟着更新，必须走这条通道。
        """
        from src.core import framework_sync
        res = framework_sync.sync(pipe.db, cfg, force=True)
        if not res.get("ok"):
            raise HTTPException(502, res.get("reason") or "同步失败")
        _fw_cache["clusterer"] = None      # 让归类预览立刻用上新框架
        if res.get("skipped"):
            res["reanalyze"] = {"skipped": True, "reason": "框架没变化，不用重算"}
        else:
            res["reanalyze"] = _fw_reanalyze()
        return res

    @app.post("/api/framework/themes")
    def api_fw_add(req: ThemeAddReq):
        try:
            repo = _fw_repo()
            repo.add(req.theme_name, req.sub_name, req.note)
            _fw_cache["clusterer"] = None
            return {"ok": True, "stats": repo.stats()}
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.put("/api/framework/themes")
    def api_fw_update(req: ThemeUpdateReq):
        try:
            repo = _fw_repo()
            n = repo.update(
                req.theme_name, req.sub_name,
                new_theme=req.new_theme, new_sub=req.new_sub,
                enabled=req.enabled, note=req.note,
            )
            _fw_cache["clusterer"] = None
            return {"ok": n > 0, "updated": n, "stats": repo.stats()}
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/framework/themes")
    def api_fw_delete(req: ThemeKeyReq):
        ok, msg = _fw_repo().delete(req.theme_name, req.sub_name)
        if ok:
            _fw_cache["clusterer"] = None
        return {"ok": ok, "message": msg}

    @app.post("/api/framework/import")
    def api_fw_import(req: ThemeImportReq):
        """重导框架文件：只替换 source='kpl' 的数据，手工添加的保留。"""
        try:
            from pathlib import Path as _P
            from src.paths import APP_DIR as _APP
            rel = req.path or cfg.get("clustering.kpl_framework_path",
                                      "config/kpl_framework.yaml")
            path = _P(rel) if _P(rel).is_absolute() else _P(_APP) / rel
            n = _fw_repo().import_yaml(path)
            _fw_cache["clusterer"] = None
            return {"ok": True, "imported": n, "stats": _fw_repo().stats()}
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.get("/api/framework/export")
    def api_fw_export():
        """导出当前全量题材树（含手工添加）为 yaml 文本。"""
        import yaml as _yaml
        tree = _fw_repo().tree(include_disabled=True)
        text = _yaml.safe_dump({"themes": tree}, allow_unicode=True,
                               sort_keys=False, width=200)
        return {"yaml": text, "count": sum(len(v) for v in tree.values())}

    @app.post("/api/framework/apply")
    def api_fw_apply(req: FwApplyReq):
        """按新框架重算指定日（默认当前数据日），让改动立刻生效。

        当日已有人工修正时跳过，避免冲掉手工排名（与 reanalyze 口径一致）。
        """
        try:
            d = _raw(req.date)
            n = pipe.db.query(
                "SELECT COUNT(*) AS c FROM leader_result "
                "WHERE trade_date=? AND is_override=1", (d.isoformat(),))[0]["c"]
            if n:
                return {"ok": False, "skipped": True,
                        "reason": f"当日有 {n} 条人工修正，未自动重算"}
            r = pipe.analyze_day(d)
            return {"ok": True, "skipped": False,
                    "themes_count": r.get("themes_count", 0),
                    "exported": r.get("exported", 0)}
        except Exception as e:
            log.exception("题材框架重算失败")
            raise HTTPException(500, str(e))

    # ---------------- 结果查询 ----------------
    @app.get("/api/dates")
    def api_dates():
        """有复盘数据的交易日列表（降序），供历史回顾选择。

        附带 is_trading_day：历史库里若有误采的休市日数据，前端能标注出来。
        """
        rows = pipe.db.query(
            "SELECT DISTINCT trade_date FROM theme_result ORDER BY trade_date DESC"
        )
        out = []
        for r in rows:
            d = date.fromisoformat(r["trade_date"])
            out.append({
                "date": r["trade_date"],
                "is_trading_day": cal.is_trading_day(d),
            })
        return out

    @app.get("/api/breadth")
    def api_breadth(date: str | None = None):
        """当日情绪宽度：涨幅>=9.90% / 涨停 / 创科北>10%。

        全市场快照只有实时一份，所以历史日期没有这三个数——返回 partial，
        前端显示「—」，不拿池子里的数糊一个出来。
        """
        d = _d(date)
        ds = d.isoformat()
        zt = pipe.db.query(
            "SELECT COUNT(*) c FROM daily_zt_pool WHERE trade_date=? AND sealed=1",
            (ds,))[0]["c"]
        # 已归纳：当天进了任一题材的股票数（去重，含概念补全进来的异动股）
        assigned = pipe.db.query(
            "SELECT COUNT(DISTINCT code) c FROM leader_result "
            "WHERE trade_date=? AND is_removed=0", (ds,))[0]["c"]
        row = pipe.db.query("SELECT * FROM market_breadth WHERE trade_date=?", (ds,))
        if row:
            r = row[0]
            return {"date": ds, "partial": False,
                    "up_99": r["up_99"], "limit_up": r["limit_up"] or zt,
                    "tech_10": r["tech_10"], "total": r["total"],
                    "assigned": assigned,
                    "captured_at": r["captured_at"], "hint": ""}
        return {"date": ds, "partial": True, "up_99": None, "limit_up": zt,
                "tech_10": None, "total": 0, "assigned": assigned,
                "captured_at": "",
                "hint": "该日没有全市场快照，只能给出涨停家数"}

    @app.get("/api/themes")
    def api_themes(date: str | None = None):
        d = _d(date)
        rows = pipe.db.query(
            "SELECT trade_date, theme_name, zt_count, strong_count, member_count, "
            "       cluster_mode, created_at FROM theme_result WHERE trade_date=? "
            "ORDER BY member_count DESC, zt_count DESC",
            (d.isoformat(),),
        )
        return [dict(r) for r in rows]

    @app.get("/api/leaders")
    def api_leaders(date: str | None = None):
        d = _d(date)
        rows = pipe.db.query(
            "SELECT * FROM leader_result WHERE trade_date=? AND is_removed=0 "
            "ORDER BY theme_name, final_rank",
            (d.isoformat(),),
        )
        return [dict(r) for r in rows]

    # ---------------- 爆量题材（成交量 >= 前N日最低量 × 2/3） ----------------
    @app.get("/api/volume/surge")
    def api_volume_surge(date: str | None = None):
        """当日爆量题材：板块排行 + 成员明细（含 2/3 两套预聚合）。

        倍数切换在前端做（零请求），左栏家数与右栏明细必然同源一致。
        没有数据时先自愈跑一次本地扫描，仍不行就把「缺哪几天」直接告诉用户。
        """
        d = _d(date)
        ds = d.isoformat()
        from src.core import volume_surge as vs

        repo = vs.VolumeRepo(pipe.db)
        if not repo.has_surge(ds):
            vs.scan_day(cfg, pipe.db, cal, d)
        out = vs.build_payload(pipe.db, ds, cfg)
        out["base_window"] = vs.prev_trading_days(cal, d, vs.vcfg(cfg)["base_window"])
        if not out.get("ready"):
            rep = vs.coverage_report(pipe.db, cal, d, cfg)
            out["lack"] = rep.get("lack") or []
            if rep.get("lack"):
                out["hint"] = ("缺少 %s 的全市场成交量，请到「更多 → 爆量回补」补一次"
                               % "、".join(rep["lack"]))
        return out

    @app.post("/api/volume/scan")
    def api_volume_scan(req: DateReq):
        """用库内成交量本地重算（改题材框架后必须跑一次，否则归属还是旧框架）。"""
        d = _d(req.date)
        from src.core import volume_surge as vs

        r = vs.scan_day(cfg, pipe.db, cal, d, force=True)
        out = vs.build_payload(pipe.db, d.isoformat(), cfg)
        out["base_window"] = vs.prev_trading_days(cal, d, vs.vcfg(cfg)["base_window"])
        out["ok"] = bool(r.get("ok"))
        if not r.get("ok"):
            out["hint"] = r.get("reason", "")
        return out

    @app.post("/api/volume/backfill")
    def api_volume_backfill(req: VolBackfillReq):
        """首次回补丢后台线程，立刻返回；进度走 GET /api/volume/job 轮询。"""
        from src.core import volume_surge as vs

        if vs.JOB.running():
            return {"ok": True, "already": True, "job": vs.JOB.snapshot()}
        days = max(1, min(int(req.days or 20), 60))
        started = vs.JOB.start(lambda: vs.backfill_history(
            pipe.db, cal, cfg, days=days, force=req.force,
            on_progress=vs.JOB.update))
        return {"ok": True, "already": not started, "job": vs.JOB.snapshot()}

    @app.get("/api/volume/job")
    def api_volume_job():
        from src.core import volume_surge as vs
        return vs.JOB.snapshot()

    @app.get("/api/isolated")
    def api_isolated(date: str | None = None):
        """独立个股题材：只有 1~2 只股、且这些股没出现在其他任何题材里的小板块。

        它们是大盘的边角料（一只股一个独家题材），从主列表拆到单独的折叠区，
        主列表只留有交集、成规模的板块。点进去仍能看梯队。
        """
        d = _d(date)
        rows = pipe.db.query(
            "SELECT * FROM leader_result WHERE trade_date=? AND is_removed=0 "
            "ORDER BY theme_name, final_rank",
            (d.isoformat(),),
        )
        by_code: dict[str, set[str]] = {}
        by_theme: dict[str, list[dict]] = {}
        for r in rows:
            by_code.setdefault(r["code"], set()).add(r["theme_name"])
            by_theme.setdefault(r["theme_name"], []).append(dict(r))

        items = []
        hidden: list[str] = []
        for theme, members in by_theme.items():
            if len(members) > 2:
                continue
            # 只挑「没在任何其他题材出现过」的股票
            lonely = [m for m in members if by_code[m["code"]] == {theme}]
            if lonely:
                items.append({
                    "theme_name": theme,
                    "count": len(lonely),
                    "members": [{
                        "code": m["code"], "name": m["name"],
                        "lianban_height": m["lianban_height"],
                        "high_pct": m["high_pct"], "zt_time": m["zt_time"],
                        "sub_tags": m.get("sub_tags") or "",
                    } for m in lonely],
                })
            else:
                # 1~2 只但成员都在别的板块露过面：整个题材从主列表收起
                hidden.append(theme)
        items.sort(key=lambda x: (-x["count"], x["theme_name"]))
        return {
            "date": d.isoformat(), "count": len(items),
            "stocks": sum(i["count"] for i in items),
            "items": items, "hidden": sorted(hidden),
        }

    @app.get("/api/pool")
    def api_pool(date: str | None = None, sealed: bool = True):
        return pipe.repo.load_zt_pool(_d(date), sealed=sealed)

    # ---------------- 个股查询（验证归类是否准确） ----------------
    @app.get("/api/theme/candidates")
    def api_theme_candidates(date: str | None = None, theme: str = ""):
        """某个题材的「补全候选」：当日池内、属于该主线相关概念但没被收进来的股票。

        核心概念（人形机器人）已经全量纳入，这里列的是**外围概念**
        （机器人概念/减速器/丝杠…）命中的个股，供人工判断是否漏了。
        """
        d = _d(date)
        theme = (theme or "").strip()
        if not theme:
            return {"date": d.isoformat(), "theme": theme, "groups": []}

        members = {
            r["code"] for r in pipe.db.query(
                "SELECT code FROM leader_result WHERE trade_date=? AND theme_name=?",
                (d.isoformat(), theme),
            )
        }
        family = cfg.get(f"clustering.theme_families.{theme}", []) or []
        core = set(cfg.get(f"clustering.theme_core_concepts.{theme}", []) or [])
        patterns = {theme, *[str(p) for p in family]}

        concepts = [c for c in pipe.repo.all_concepts()
                    if c not in core and any(p in c for p in patterns)]
        groups = []
        for c in concepts:
            codes = pipe.repo.members_of(c)
            rows = []
            for code in codes:
                if code in members:
                    continue
                r = pipe.db.query(
                    "SELECT * FROM daily_zt_pool WHERE trade_date=? AND code=? "
                    "AND sealed IN (1,2) ORDER BY sealed LIMIT 1",
                    (d.isoformat(), code),
                )
                if not r:
                    continue
                rows.append({
                    "code": code, "name": r[0]["name"],
                    "status": "涨停" if r[0]["sealed"] == 1 else "创/科异动",
                    "reason": r[0]["reason"] or "",
                    "high_pct": r[0]["high_pct"] or 0.0,
                    "lianban_height": r[0]["lianban_height"] or 0,
                })
            if rows:
                rows.sort(key=lambda x: (-x["lianban_height"], -x["high_pct"]))
                groups.append({"concept": c, "count": len(rows), "stocks": rows})
        groups.sort(key=lambda g: -g["count"])
        return {
            "date": d.isoformat(), "theme": theme,
            "core_concepts": sorted(core), "groups": groups,
            "total": len({s["code"] for g in groups for s in g["stocks"]}),
        }

    @app.get("/api/unassigned")
    def api_unassigned(date: str | None = None):
        """「未归纳」清单：当日涨停股 + 创/科异动股里，没有归入任何题材的。

        口径：只要该股出现在任一题材（member_count>=1 即可），就算已归纳，不再列出。
        min_cluster_size=1，所以「只有它自己一只」的孤股也算有题材——它的题材在左栏，
        用户明确要求不要在这里重复出现。
        """
        d = _d(date)
        rows = pipe.db.query(
            "SELECT p.code, p.name, p.sealed, p.reason, "
            "(SELECT GROUP_CONCAT(c.concept_name) FROM concept_member c "
            "  WHERE c.code=p.code) AS concepts, "
            "p.lianban_height, p.high_pct, p.zt_time "
            "FROM daily_zt_pool p "
            "WHERE p.trade_date=? AND p.sealed IN (1,2) "
            "  AND p.code NOT IN ("
            "    SELECT l.code FROM leader_result l "
            "    WHERE l.trade_date=? AND l.is_removed=0"
            "  ) "
            "ORDER BY p.sealed, p.lianban_height DESC, p.zt_time",
            (d.isoformat(), d.isoformat()),
        )
        out = [{
            "code": r["code"], "name": r["name"],
            "status": "涨停" if r["sealed"] == 1 else "创/科异动",
            "reason": r["reason"] or "",
            "concepts": (r["concepts"] or "").split(",") if r["concepts"] else [],
            "lianban_height": r["lianban_height"] or 0,
            "high_pct": r["high_pct"] or 0.0,
            "zt_time": r["zt_time"] or "",
        } for r in rows]
        return {"date": d.isoformat(), "count": len(out), "stocks": out}

    @app.get("/api/stock/search")
    def api_stock_search(q: str, date: str | None = None):
        """按代码/名称查个股：涨停原因、连板、涨幅、归到哪些题材、同花顺概念。

        池内查不到时回退全市场名称表，明确告诉用户「当日未涨停/未进入异动池」，
        避免误以为是归类漏了。
        """
        d = _d(date)
        q = (q or "").strip()
        if not q:
            return {"date": d.isoformat(), "query": q, "results": []}

        def payload(row: dict, in_pool: bool = True) -> dict:
            code = row["code"]
            themes = pipe.db.query(
                "SELECT l.theme_name, l.final_rank, l.algo_rank, l.leader_label, "
                "       l.is_override, t.zt_count "
                "FROM leader_result l "
                "LEFT JOIN theme_result t ON t.trade_date=l.trade_date "
                "  AND t.theme_name=l.theme_name "
                "WHERE l.trade_date=? AND l.code=? AND l.is_removed=0 "
                "ORDER BY t.member_count DESC, l.final_rank",
                (d.isoformat(), code),
            )
            concepts = pipe.repo.concepts_of(code)
            sealed = row.get("sealed")
            return {
                "code": code, "name": row.get("name", ""),
                "in_pool": in_pool,
                "sealed": sealed,
                "status": {1: "涨停", 2: "创/科异动", 0: "炸板"}.get(sealed, "未涨停"),
                "reason": row.get("reason") or "",
                "anomaly_tag": row.get("anomaly_tag") or "",
                "anomaly_content": row.get("anomaly_content") or "",
                "lianban_height": row.get("lianban_height") or 0,
                "zt_time": row.get("zt_time") or "",
                "high_pct": row.get("high_pct") or 0.0,
                "turnover": row.get("turnover") or 0.0,
                "float_mv": row.get("float_mv") or 0.0,
                "themes": [dict(t) for t in themes],
                "concepts": concepts[:20],
                "concept_total": len(concepts),
            }

        rows = pipe.db.query(
            "SELECT * FROM daily_zt_pool WHERE trade_date=? AND "
            "(code=? OR name=? OR code LIKE ? OR name LIKE ?) "
            "ORDER BY sealed DESC, code LIMIT 20",
            (d.isoformat(), q, q, f"%{q}%", f"%{q}%"),
        )
        # 精确命中排前面
        rows = sorted(rows, key=lambda r: (0 if q in (r["code"], r["name"]) else 1, r["code"]))
        results, seen = [], set()
        for r in rows:
            if r["code"] in seen:
                continue
            seen.add(r["code"])
            results.append(payload(dict(r), True))
        if results:
            return {"date": d.isoformat(), "query": q, "results": results}

        # 不在当日池：查全市场名称表，判断是「没涨停」还是「查无此股」
        names: dict = {}
        try:
            names = fy.all_a_share_names() or {}
        except Exception as e:
            log.warning("全市场名称表拉取失败: %s", e)
        hits = []
        for ticker, name in names.items():
            code = str(ticker).split(".")[0]
            if q == code or (len(q) >= 2 and q in name):
                hits.append((code, name))
        for code, name in hits[:8]:
            results.append(payload({"code": code, "name": name}, False))
        return {
            "date": d.isoformat(),
            "query": q,
            "results": results,
            "note": "" if results else "查无此股（代码/名称都未匹配）",
        }

    @app.get("/api/stock/card")
    def api_stock_card(code: str, date: str | None = None):
        """梯队里鼠标悬停用的个股卡片。

        只放库里真实有的数据：涨停原因（同花顺原始文本）、所属题材与梯队位置、
        同题材共性词、同题材其它成员、同花顺概念、盘口。
        没有「行业共性」那段新闻催化——同花顺悬浮卡里那是它自家的资讯库，
        我们的数据源（涨停池 + 概念成分）没有，不编造。
        """
        d = _d(date)
        ds = d.isoformat()
        code = (code or "").strip()
        if not code:
            raise HTTPException(400, "缺少股票代码")

        # 同一代码可能同时有封板/炸板记录，按 涨停 > 异动 > 炸板 取一条
        rows = pipe.db.query(
            "SELECT code,name,sealed,reason,lianban_height,high_pct,zt_time,"
            "       turnover,float_mv,fengdan_amt "
            "FROM daily_zt_pool WHERE trade_date=? AND code=? "
            "ORDER BY CASE sealed WHEN 1 THEN 0 WHEN 2 THEN 1 ELSE 2 END LIMIT 1",
            (ds, code),
        )
        if not rows:
            raise HTTPException(404, f"{ds} 池内没有 {code}")
        r = rows[0]

        themes = pipe.db.query(
            "SELECT l.theme_name, l.final_rank, l.leader_label, "
            "       t.member_count, t.zt_count "
            "FROM leader_result l JOIN theme_result t "
            "  ON t.trade_date=l.trade_date AND t.theme_name=l.theme_name "
            "WHERE l.trade_date=? AND l.code=? AND l.is_removed=0 "
            "ORDER BY t.member_count DESC, l.final_rank",
            (ds, code),
        )

        stopwords = set(cfg.get("clustering.reason_stopwords", []) or [])
        peers: list[dict] = []
        common: list[dict] = []
        if themes:
            members = pipe.db.query(
                "SELECT l.code, l.name, l.final_rank, l.leader_label, "
                "       l.lianban_height, p.reason "
                "FROM leader_result l LEFT JOIN daily_zt_pool p "
                "  ON p.trade_date=l.trade_date AND p.code=l.code AND p.sealed=1 "
                "WHERE l.trade_date=? AND l.theme_name=? AND l.is_removed=0 "
                "ORDER BY l.final_rank",
                (ds, themes[0]["theme_name"]),
            )
            common = _common_keywords([dict(m) for m in members], stopwords)
            peers = [{"code": m["code"], "name": m["name"],
                      "lianban": m["lianban_height"] or 0,
                      "label": m["leader_label"] or "",
                      "rank": m["final_rank"] or 0}
                     for m in members if m["code"] != code][:6]

        black = set(cfg.get("clustering.generic_concept_blacklist", []) or [])
        cpt_rows = pipe.db.query(
            "SELECT c.concept_name AS name, s.cnt AS cnt FROM concept_member c "
            "JOIN (SELECT concept_name, COUNT(*) cnt FROM concept_member "
            "      GROUP BY concept_name) s ON s.concept_name=c.concept_name "
            "WHERE c.code=? ORDER BY s.cnt ASC",
            (code,),
        )
        concepts = [c["name"] for c in cpt_rows if c["name"] not in black][:10]

        # 最近 N 次涨停（含当日）：未归纳的票最需要看这个，判断是不是反复做的老票
        recent = pipe.db.query(
            "SELECT trade_date, reason, zt_time, lianban_height "
            "FROM daily_zt_pool WHERE code=? AND sealed=1 AND trade_date<=? "
            "ORDER BY trade_date DESC LIMIT 4",
            (code, ds),
        )

        fmv = r["float_mv"] or 0.0
        fd = r["fengdan_amt"] or 0.0
        sealed = r["sealed"]
        return {
            "date": ds,
            "code": r["code"],
            "name": r["name"],
            "status": "涨停" if sealed == 1 else ("炸板" if sealed == 0 else "创/科异动"),
            "sealed": sealed,
            "high_pct": r["high_pct"] or 0.0,
            "lianban_height": r["lianban_height"] or 0,
            "reason": r["reason"] or "",
            "keywords": _reason_keywords(r["reason"] or "", stopwords),
            "themes": [{"name": t["theme_name"], "rank": t["final_rank"] or 0,
                        "label": t["leader_label"] or "",
                        "member_count": t["member_count"] or 0} for t in themes],
            "common": common,
            "peers": peers,
            "concepts": concepts,
            "concept_total": len(cpt_rows),
            "recent": [{"date": x["trade_date"], "reason": x["reason"] or "",
                        "zt_time": x["zt_time"] or "",
                        "lianban": x["lianban_height"] or 0} for x in recent],
            "fengdan_pct": round(fd / fmv * 100, 2) if fmv else 0.0,
            "turnover": r["turnover"] or 0.0,
            "zt_time": r["zt_time"] or "",
            "float_mv": fmv,
            "note": notes.get("stock", ds, r["code"]),
        }

    # ---------------- 复盘笔记（日 / 题材 / 个股三级） ----------------
    @app.get("/api/notes")
    def api_notes(date: str | None = None, scope: str | None = None,
                  key: str | None = None):
        """当日三层笔记一次拿齐，切日期只多一个请求。

        带 scope+key 时（切页签）额外返回该题材/个股的历史脉络，避免每天重复写。
        """
        d = _d(date)
        ds = d.isoformat()
        out = notes.bundle(ds)
        out["date"] = ds
        out["count"] = notes.count(ds)
        out["history"] = (notes.ref_history(scope, key, ds)
                          if scope in ("theme", "stock") and key else [])
        return out

    @app.post("/api/note")
    def api_note_save(req: NoteReq):
        d = _d(req.date)
        try:
            r = notes.save(req.scope, d.isoformat(), req.key, req.content)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "date": d.isoformat(), **r}

    # ---------------- 龙头人工调节（override 层） ----------------
    @app.post("/api/override/rerank")
    def api_rerank(req: RerankReq):
        try:
            ovr.rerank(_d(req.date), req.theme, req.code, req.new_rank)
            return {"ok": True}
        except KeyError as e:
            raise HTTPException(404, str(e))

    @app.post("/api/override/move_out")
    def api_move_out(req: MoveOutReq):
        ovr.move_out(_d(req.date), req.theme, req.code, req.reason)
        return {"ok": True}

    @app.post("/api/override/add_to_theme")
    def api_add_to_theme(req: AddToThemeReq):
        """把未归纳的个股手动归纳进题材（人工层，不影响算法排名）。"""
        d = _d(req.date)
        ds = d.isoformat()
        row = pipe.db.query(
            "SELECT name, lianban_height FROM daily_zt_pool "
            "WHERE trade_date=? AND code=? "
            "ORDER BY CASE sealed WHEN 1 THEN 0 WHEN 2 THEN 1 ELSE 2 END LIMIT 1",
            (ds, req.code))
        if not row:
            raise HTTPException(404, f"{ds} 池内没有 {req.code}")
        try:
            added = ovr.add_to_theme(d, req.theme, req.code,
                                     row[0]["name"] or "", row[0]["lianban_height"] or 1)
        except KeyError as e:
            raise HTTPException(404, str(e))
        return {"ok": True, "added": added,
                "hint": "" if added else f"{row[0]['name']} 本来就在「{req.theme}」里"}

    @app.post("/api/override/reset_theme")
    def api_reset_theme(req: ResetThemeReq):
        return {"restored": ovr.reset_theme(_d(req.date), req.theme)}

    @app.post("/api/override/reset_day")
    def api_reset_day(req: DateReq):
        return {"restored": ovr.reset_day(_d(req.date))}

    @app.get("/api/override/count")
    def api_interventions(date: str | None = None):
        return {"count": ovr.daily_intervention_count(_d(date))}

    # ---------------- 导出 ----------------
    @app.post("/api/clipboard")
    def api_clipboard(req: DateReq):
        d = _d(req.date)
        sql = ("SELECT l.code FROM leader_result l "
               "JOIN theme_result t ON t.trade_date=l.trade_date "
               "  AND t.theme_name=l.theme_name "
               "WHERE l.trade_date=? AND l.is_removed=0 ")
        params: tuple = (d.isoformat(),)
        if req.theme:
            sql += "AND l.theme_name=? "
            params += (req.theme,)
        sql += "ORDER BY t.member_count DESC, t.zt_count DESC, l.final_rank"
        rows = pipe.db.query(sql, params)
        if not rows:
            raise HTTPException(404, f"{d} 无可导出的题材成分股")
        groups = build_code_groups([dict(r) for r in rows], cfg)
        if not groups:
            raise HTTPException(404, f"{d} 无可导出的题材成分股")
        idx = max(0, min(req.group_index or 0, len(groups) - 1))
        try:
            copy_to_clipboard(groups[idx])
        except ClipboardUnavailable as e:
            raise HTTPException(503, str(e))
        sep = "\n" if cfg.get("export.clipboard_separator", "newline") == "newline" else ","
        return {
            "group_text": groups[idx],
            "group_index": idx,
            "total_groups": len(groups),
            "group_sizes": [g.count(sep) + 1 for g in groups],
            "count": len({r["code"] for r in rows}),
        }

    # ---------------- 导出到同花顺（txt / 图片） ----------------
    @app.post("/api/export/ths")
    def api_export_ths(req: ExportThsReq):
        d = _d(req.date)
        scope = (req.scope or "single").lower()
        if scope not in {"single", "all"}:
            raise HTTPException(400, f"未知的导出范围: {req.scope}")
        if scope == "single" and not req.theme:
            raise HTTPException(400, "请先在左侧选择题材")

        rows = ths.collect_codes(pipe.db, d, req.theme if scope == "single" else None)
        if not rows:
            hint = (f"{d} 无复盘数据，请先点「开始复盘」" if scope == "all"
                    else f"{d}「{req.theme}」无成分股，请先复盘或检查题材名")
            return {"ok": True, "date": d.isoformat(), "dir": "", "files": [], "images": [],
                    "skipped": [], "total": 0, "themes": 0, "hint": hint}

        groups = ths.group_by_theme(rows)
        skipped: list[dict] = []
        if scope == "all":
            floor = 1 if req.include_single else int(cfg.get("export.ths.min_members", 2) or 1)
            groups, skipped = ths.filter_themes(
                groups, floor, int(cfg.get("export.ths.max_themes", 30) or 0))
        if not groups:
            return {"ok": True, "date": d.isoformat(), "dir": "", "files": [], "images": [],
                    "skipped": skipped, "total": 0, "themes": 0,
                    "hint": "筛选后没有可导出的题材（只含 1 只的题材默认跳过，可勾选放开）"}

        day = ths.day_dir(cfg, d.isoformat())
        txt_chunk = int(cfg.get("export.ths.txt_chunk", 0) or 0)
        img_chunk = int(cfg.get("export.ths.img_chunk", 12) or 12)
        safe = ths.unique_names([th for th, _ in groups])

        files: list[dict] = []
        images: list[dict] = []
        for th, codes in groups:
            for b in ths.plan_txt_batches(safe[th], codes, d.isoformat(), txt_chunk):
                p = ths.write_txt_file(day / b["name"], b["codes"])
                files.append({"name": b["name"], "kind": "txt", "index": b["index"],
                              "total": b["total"], "count": b["count"], "theme": th,
                              "path": str(p)})
            if scope == "single":
                for b in ths.plan_image_batches(safe[th], codes, d.isoformat(), img_chunk):
                    images.append({"name": b["name"], "index": b["index"], "total": b["total"],
                                   "count": b["count"], "codes": b["codes"], "theme": th,
                                   "path": str(day / b["name"])})

        if scope == "all":
            merged = ths.global_dedup(groups)
            name = f"00_全部题材_{d.isoformat()}.txt"
            p = ths.write_txt_file(day / name, merged)
            files.insert(0, {"name": name, "kind": "txt", "index": 1, "total": 1,
                             "count": len(merged), "theme": "全部题材（合并去重）",
                             "path": str(p)})

        # 只有在整批导出时才清理其它题材的旧 txt；单独导一个题材不该动别人的文件
        if scope == "all":
            ths.delete_stale_txt(day, {f["name"] for f in files})
        ths.prune_old_dirs(ths.export_root(cfg), int(cfg.get("export.ths.keep_days", 7) or 0))

        return {
            "ok": True,
            "date": d.isoformat(),
            "dir": str(day),
            "files": files,
            "images": images,
            "skipped": skipped,
            "total": len({c for _, cs in groups for c in cs}),
            "themes": len(groups),
            "hint": "",
            "img_opt": {
                "font": int(cfg.get("export.ths.img_font_px", 40) or 40),
                "line": int(cfg.get("export.ths.img_line_px", 56) or 56),
                "pad": int(cfg.get("export.ths.img_pad_px", 36) or 36),
                "dpr": float(cfg.get("export.ths.img_dpr", 2) or 2),
            },
        }

    @app.post("/api/export/ths/image")
    def api_export_ths_image(req: ImageUploadReq):
        if not req.items:
            raise HTTPException(400, "没有要保存的图片")
        if len(req.items) > 40:
            raise HTTPException(400, "一次最多保存 40 张图片")
        roots = ths.allowed_roots(cfg)
        saved: list[str] = []
        failed: list[dict] = []
        for it in req.items:
            try:
                p = ths.ensure_inside_any(Path(it.path), roots)
                if p.suffix.lower() != ".png":
                    raise ValueError("只接受 .png")
                raw = base64.b64decode((it.data_b64 or "").split(",", 1)[-1], validate=True)
                if not raw:
                    raise ValueError("图片内容为空")
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError("单张超过 2MB")
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(raw)
                saved.append(p.name)
            except Exception as e:  # 单张失败不影响其他张
                failed.append({"name": Path(it.path).name, "error": str(e)})
        log.info("同花顺图片落盘 %d 张，失败 %d 张", len(saved), len(failed))
        return {"ok": not failed, "saved": len(saved), "failed": failed}

    @app.post("/api/export/open")
    def api_export_open(req: OpenThsReq):
        """打开某日的导出目录。

        目录由后端按日期解析，不依赖前端 state——页面刷新后 thsState.dir 是空的，
        前端拿空路径去点按钮会「没反应」，这是之前那个问题的根因。
        """
        d = _d(req.date)
        day = ths.peek_day_dir(cfg, d.isoformat())
        if not day.is_dir() or not any(day.iterdir()):
            return {"ok": False, "dir": str(day),
                    "hint": f"{d} 还没有导出记录，请先点「生成 TXT」"}
        # 选中最新生成的文件而不是只开目录：窗口会被带到前台，也直接看出该传哪个文件
        files = sorted((p for p in day.iterdir() if p.is_file()),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if ths.reveal_in_explorer(files[0] if files else day):
            return {"ok": True, "dir": str(day), "hint": ""}
        return {"ok": False, "dir": str(day), "hint": f"无法自动打开，路径：{day}"}

    @app.post("/api/export/reveal")
    def api_export_reveal(req: RevealReq):
        try:
            p = ths.ensure_inside_any(Path(req.path), ths.allowed_roots(cfg))
        except ValueError:
            raise HTTPException(400, "路径越界")
        if not p.exists():
            raise HTTPException(404, f"文件不存在：{p.name}")
        if ths.reveal_in_explorer(p):
            return {"ok": True, "hint": ""}
        return {"ok": False, "hint": f"已写入 {p.parent}，请手动打开该文件夹"}

    # 打包版（发给朋友的 exe）启动时静默同步一次框架。
    # 源码运行刻意不触发——否则会把你本地还没发布的框架改动冲掉。
    if is_frozen() and cfg.get("update.enabled", True) \
            and cfg.get("update.framework_auto_sync", True):
        def _bg_fw_sync():
            import time
            time.sleep(6)                     # 等服务先起来，别抢启动
            try:
                from src.core import framework_sync
                res = framework_sync.sync(pipe.db, cfg)
                if res.get("ok") and not res.get("skipped"):
                    _fw_cache["clusterer"] = None
                    ra = _fw_reanalyze()
                    log.info("启动时已同步框架：%s | 当天重算：%s", res, ra)
            except Exception:
                log.debug("启动时框架同步失败（忽略）", exc_info=True)

        threading.Thread(target=_bg_fw_sync, daemon=True).start()

    return app
