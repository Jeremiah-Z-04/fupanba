"""ThemeHunter / 复盘吧 - CLI 入口

用法:
  python main.py fetch [--date 2026-10-22]     盘后采集涨停池+炸板池+连板更新
  python main.py concepts [--force]            刷新静态概念成分缓存（周更）
  python main.py run [--date ...] [--no-clip]  盘后复盘全流程（采集→聚类→龙标→导出）
  python main.py report [--date ...]           查看某日结果
  python main.py reanalyze [--days N] [--force] 用当前聚类配置重算历史题材
  python main.py serve [--port 8765]           启动网页操作台（任务8）
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from src.config import Config
from src.log import get_logger, setup
from src.pipeline import Pipeline


def parse_date(s: str | None) -> date:
    if not s:
        return date.today()
    return date.fromisoformat(s)


def main() -> int:
    parser = argparse.ArgumentParser(prog="themehunter", description="短线题材自动复盘引擎")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="盘后采集")
    p_fetch.add_argument("--date", default=None)

    p_concepts = sub.add_parser("concepts", help="刷新概念成分缓存")
    p_concepts.add_argument("--force", action="store_true")

    p_backfill = sub.add_parser("backfill", help="历史回填（默认近30交易日）")
    p_backfill.add_argument("--days", type=int, default=30)

    p_run = sub.add_parser("run", help="盘后复盘全流程")
    p_run.add_argument("--date", default=None)
    p_run.add_argument("--no-clip", action="store_true", help="不写剪贴板")

    p_report = sub.add_parser("report", help="查看结果")
    p_report.add_argument("--date", default=None)

    p_re = sub.add_parser("reanalyze", help="用当前聚类配置重算历史题材（改了归并/停用词后必须跑）")
    p_re.add_argument("--days", type=int, default=0, help="最近 N 个交易日，0=全部")
    p_re.add_argument("--force", action="store_true", help="连有生效人工修正的日期也重算")

    p_serve = sub.add_parser("serve", help="启动网页操作台")
    p_serve.add_argument("--port", type=int, default=8765)

    p_volfill = sub.add_parser("volfill", help="回补全市场逐日成交量（爆量组件首次必跑）")
    p_volfill.add_argument("--days", type=int, default=20)
    p_volfill.add_argument("--force", action="store_true", help="连已有数据的股票也重拉")

    p_volscan = sub.add_parser("volscan", help="用库内成交量重算当日爆量题材")
    p_volscan.add_argument("--date", default=None)
    p_volscan.add_argument("--days", type=int, default=0,
                           help="最近 N 个交易日全重算，0=只算当日（改了题材框架后用它）")

    args = parser.parse_args()

    cfg = Config.load()
    setup(cfg.data_dir / "logs")
    log = get_logger("main")

    if args.cmd == "serve":
        try:
            import uvicorn
            from src.server.app import create_app
        except ImportError:
            print("网页操作台依赖未安装或尚未实现（MVP1 后期提供）")
            return 1
        uvicorn.run(create_app(), host="127.0.0.1", port=args.port)
        return 0

    pipe = Pipeline(cfg)

    if args.cmd == "fetch":
        d = parse_date(args.date)
        result = pipe.fetch(d)
        print(f"[{d}] 涨停 {result['zt']} 只 | 炸板 {result['zhaban']} 只 | "
              f"连板在册 {result['lianban']} 只")

    elif args.cmd == "concepts":
        n = pipe.refresh_concepts(force=args.force)
        print("概念成分已是最新" if n < 0 else f"概念板块刷新完成: {n} 个")

    elif args.cmd == "backfill":
        result = pipe.backfill(days=args.days)
        print(f"历史回填完成：{len(result['done'])} 个交易日"
              f"（跳过已有 {result['skipped']}，共 {result['total']}）")
        if result.get("empty"):
            print(f"区间无涨停数据 {len(result['empty'])} 天："
                  f"{', '.join(result['empty'][:12])}")
        if result.get("failed"):
            print(f"失败 {len(result['failed'])} 天（直接重跑会只补这些）：")
            for f in result["failed"][:12]:
                print(f"  {f['date']}  {f['error'][:90]}")

    elif args.cmd == "run":
        d = parse_date(args.date)
        result = pipe.run(d, to_clipboard=not args.no_clip)
        print(f"\n===== {d} 复盘结果 =====")
        for theme, stat in result.get("themes", {}).items():
            extra = f"（另创/科异动 {stat['strong_count']} 只）" if stat.get("strong_count") else ""
            print(f"\n【{theme}】涨停 {stat['zt_count']} 只{extra}")
            for r in result["leaders"]:
                if r["theme_name"] == theme:
                    print(f"  {r['leader_label']} {r['code']} {r['name']} "
                          f"{r.get('lianban_height', 1)}板 涨幅 {r.get('high_pct', 0):.2f}%")
        print(f"\n导出 {result['exported']} 只 | CSV: {result.get('csv')}")
        print("剪贴板: " + ("已写入，同花顺自定义板块直接 Ctrl+V" if result.get("clipboard") else "未写入"))

    elif args.cmd == "reanalyze":
        r = pipe.reanalyze(days=args.days, force=args.force)
        print(f"重算完成：{len(r['done'])}/{r['total']} 个交易日")
        if r["skipped"]:
            print(f"跳过（存在人工修正，加 --force 可覆盖）：{', '.join(r['skipped'])}")
        for f in r["failed"]:
            print(f"失败 {f['date']}: {f['error']}")

    elif args.cmd == "report":
        d = parse_date(args.date)
        rows = pipe.db.query(
            "SELECT * FROM leader_result WHERE trade_date=? ORDER BY theme_name, final_rank",
            (d.isoformat(),),
        )
        if not rows:
            print(f"{d} 无复盘结果，先执行: python main.py run --date {d}")
            return 1
        for r in rows:
            mark = "（人工修正）" if r["is_override"] else ""
            print(f"{r['theme_name']} | 龙{r['final_rank']} {r['code']} {r['name']} "
                  f"{r['lianban_height']}板{mark}")

    elif args.cmd == "volfill":
        from src.core import volume_surge as vs

        def _progress(s):
            print("\r  回补中 %d/%d  成功 %d 失败 %d  写入 %d 行   "
                  % (s["done"], s["total"], s["ok"], s["fail"], s["rows"]),
                  end="", flush=True)

        r = vs.backfill_history(pipe.db, pipe.cal, cfg, days=args.days,
                                force=args.force, on_progress=_progress)
        if not r["total"]:
            print("已全部回补完成，无需重跑（加 --force 可强制重拉）")
        else:
            print("\n回补完成：成功 %d 只 / 区间无K线 %d 只 / 失败 %d 只，写入 %d 行"
                  % (r["ok"], r["empty"], r["fail"], r["rows"]))
            if r["failed"]:
                print("失败示例（再跑一次会自动只补这些）：%s"
                      % ", ".join(r["failed"][:15]))

    elif args.cmd == "volscan":
        from src.core import volume_surge as vs

        d = pipe._resolve_day(parse_date(args.date))["trade_date"]
        if args.days and args.days > 0:
            dates = [x for x in pipe.cal.days() if x <= d][-args.days:]
        else:
            dates = [d]

        ok = skip = 0
        for dd in dates:
            r = vs.scan_day(cfg, pipe.db, pipe.cal, dd, force=True)
            if r.get("ok"):
                ok += 1
                if len(dates) > 1:
                    print(f"  {dd} 命中 {r['surge']} 只（≥2倍 {r['show']}）→ {r['themes']} 个板块")
            else:
                skip += 1
                if len(dates) > 1:
                    print(f"  {dd} 跳过: {r.get('reason')}")
        if len(dates) > 1:
            print(f"\n重算完成：成功 {ok} / 跳过 {skip} 个交易日")
        if not ok:
            print("没有任何交易日可用，先跑：python main.py volfill")
            return 1

        payload = vs.build_payload(pipe.db, d.isoformat(), cfg)
        if not payload.get("ready"):
            print(f"{d} 无爆量结果: {payload.get('hint')}")
            return 1
        m = payload["mults"][0]
        print(f"===== {d} 爆量题材（≥{m}倍）=====")
        print(f"命中 {payload['totals'].get('surge_%d' % m, 0)} 只，"
              f"归入 {len(payload['themes'])} 个板块，"
              f"未归类 {payload['totals'].get('unmapped_%d' % m, 0)} 只")
        for t in payload["themes"][:15]:
            print(f"  {t['theme_name']:<12} {t['count_%d' % m]:>3} 只  "
                  f"最高 {t['max_%d' % m]:.2f} 倍  （其中涨停 {t['zt_2']} 只）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
