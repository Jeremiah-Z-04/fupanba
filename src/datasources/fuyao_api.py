# -*- coding: utf-8 -*-
"""同花顺官方金融数据 API 客户端（fuyao.aicubes.cn）—— 项目内置版。

从用户参考实现移植（E:\\投资工具自用\\fuyao_api.py），零第三方依赖，仅 urllib。
设计原则：
  1. 只做「取数 + 归一化」，失败统一返回 None，由上层决定是否降级。
  2. 不抛异常给上层，保证主链路不因外部接口抖动而崩。
  3. API Key 只从环境变量 / 项目根目录 .env 读取，绝不写进代码或日志。
  4. 分类 TTL 缓存，控制请求频率（官方要求）。

响应信封：{"code":0, "message":"success", "request_id":"...", "data":{...}}
code != 0 为业务错误；HTTP 429 或 code==4001 为限流。
"""
import os
import json
import time
import threading
import datetime
import urllib.request
import urllib.parse
import urllib.error

# 项目根目录（src/datasources/fuyao_api.py 上溯两层）
_DIR = os.path.dirname(os.path.abspath(__file__))
for _ in range(2):
    _DIR = os.path.dirname(_DIR)
DIR = _DIR

BASE_URL = "https://fuyao.aicubes.cn"

# 上海时区（固定 +08:00，A 股无夏令时，避免 zoneinfo/tzdata 依赖）
_TZ_SH = datetime.timezone(datetime.timedelta(hours=8))

try:
    import ssl
    _ssl_ctx = ssl.create_default_context()
except Exception:
    _ssl_ctx = None

# ---------------------------------------------------------------- API Key

_ENV_KEYS = ("HITHINK_FINANCE_API_KEY", "FUYAO_API_KEY", "HITHINK_API_KEY")
_api_key_cache = None
_api_key_lock = threading.Lock()


def _load_dotenv_api_key():
    # 打包后 .env 放在 exe 同级；源码运行时放项目根
    try:
        from src.paths import APP_DIR
        candidates = [os.path.join(str(APP_DIR), ".env"), os.path.join(DIR, ".env")]
    except Exception:
        candidates = [os.path.join(DIR, ".env")]
    seen = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() in _ENV_KEYS:
                        return v.strip().strip('"').strip("'")
        except Exception:
            continue
    return None


def get_api_key():
    global _api_key_cache
    if _api_key_cache:
        return _api_key_cache
    with _api_key_lock:
        if _api_key_cache:
            return _api_key_cache
        for k in _ENV_KEYS:
            v = os.environ.get(k)
            if v and v.strip():
                _api_key_cache = v.strip()
                return _api_key_cache
        v = _load_dotenv_api_key()
        if v:
            _api_key_cache = v
        return _api_key_cache


def is_configured():
    return bool(get_api_key())


# ---------------------------------------------------------------- 缓存

_CACHE = {}
_CACHE_LOCK = threading.Lock()

_TTL = {
    "pool": 60, "snapshot": 30, "market_all": 25,
    "calendar": 43200, "catalog": 43200, "ladder": 300, "kline": 600,
    "anomaly": 120,
}


def _cache_get(key):
    with _CACHE_LOCK:
        item = _CACHE.get(key)
    if not item:
        return None, True
    ts, ttl, val = item
    return val, (time.time() - ts > ttl)


def _cache_set(key, val, ttl):
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), ttl, val)


# ---------------------------------------------------------------- HTTP

last_error = {"code": None, "message": "", "at": 0, "path": ""}


def _record_error(code, message, path):
    last_error.update({
        "code": code, "message": str(message)[:200],
        "at": time.time(), "path": path,
    })


def _request(path, params=None, timeout=8, retries=1, ttl_group="pool", force=False):
    key = None
    if ttl_group:
        qs = urllib.parse.urlencode(sorted((params or {}).items()))
        key = path + "?" + qs
        if not force:
            val, stale = _cache_get(key)
            if val is not None and not stale:
                return val

    api_key = get_api_key()
    if not api_key:
        _record_error(-1, "API key not configured", path)
        return None

    url = BASE_URL + path
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)

    ttl = _TTL.get(ttl_group, 60) if ttl_group else 0

    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "X-api-key": api_key,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept": "*/*",
            })
            resp = urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx)
            raw = resp.read().decode("utf-8")
            payload = json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            _record_error(429 if e.code == 429 else -2, "HTTP " + str(e.code), path)
            return None
        except Exception as e:
            if attempt < retries:
                time.sleep(0.4 * (attempt + 1))
                continue
            _record_error(-3, str(e), path)
            return None

        code = payload.get("code")
        if code == 0:
            data = payload.get("data")
            if key and data is not None:
                _cache_set(key, data, ttl)
            return data
        if code == 4001 and attempt < retries:
            time.sleep(1.0 * (attempt + 1))
            continue
        _record_error(code, payload.get("message", ""), path)
        return None
    return None


# ---------------------------------------------------------------- 时间工具

def sh_date_ms(dt=None):
    if dt is None:
        dt = datetime.datetime.now(_TZ_SH)
    if isinstance(dt, str):
        for fmt in ("%Y-%m-%d", "%Y%m%d"):
            try:
                dt = datetime.datetime.strptime(dt, fmt).replace(tzinfo=_TZ_SH)
                break
            except ValueError:
                continue
    if isinstance(dt, datetime.datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_TZ_SH)
    else:
        return None
    return int(dt.timestamp() * 1000)


def latest_trading_day(date=None, force=False):
    days = trading_days(force=force)
    if not days:
        return None
    if date is None:
        target = datetime.datetime.now(_TZ_SH).strftime("%Y%m%d")
    elif isinstance(date, str):
        target = date.replace("-", "")
    elif isinstance(date, datetime.datetime):
        target = date.strftime("%Y%m%d")
    else:
        return None
    prev = [d for d in days if d <= target]
    return max(prev) if prev else None


def resolve_date_ms(date_str=None, force=False):
    if date_str:
        return sh_date_ms(date_str)
    now = datetime.datetime.now(_TZ_SH)
    today = now.strftime("%Y%m%d")
    days = trading_days(force=force)
    if days and today not in set(days):
        ltd = latest_trading_day(today, force=force)
        if ltd:
            return sh_date_ms(ltd)
    return sh_date_ms(now)


# ---------------------------------------------------------------- 涨跌停池

def _fetch_pool(path, date_ms=None, sort_field=None, sort_dir="desc",
                max_pages=3, force=False):
    if date_ms is None:
        date_ms = sh_date_ms()
    items = []
    page = 1
    while page <= max_pages:
        params = {"date_ms": date_ms, "page": page, "size": 200}
        if sort_field:
            params["sort_field"] = sort_field
            params["sort_dir"] = sort_dir
        data = _request(path, params, timeout=10, ttl_group="pool", force=force)
        if not data:
            return None if page == 1 else items
        batch = data.get("item") or []
        items.extend(batch)
        pagination = data.get("pagination") or {}
        total_pages = pagination.get("pages") or 1
        if page >= total_pages or not batch:
            break
        page += 1
    return items


def limit_up_pool(date_ms=None, force=False):
    return _fetch_pool("/api/a-share/special-data/limit-up-pool",
                       date_ms=date_ms, sort_field="continue_day_cnt",
                       sort_dir="desc", force=force)


def limit_break_pool(date_ms=None, force=False):
    return _fetch_pool("/api/a-share/special-data/limit-break-pool",
                       date_ms=date_ms, sort_field="price_change_ratio_pct",
                       sort_dir="desc", force=force)


def limit_up_ladder(force=False):
    return _request("/api/a-share/special-data/limit-up-ladder", None,
                    timeout=12, ttl_group="ladder", force=force)


# ---------------------------------------------------------------- 异动原因

_ANOMALY_TAGS = ("SHARP_RISE", "RAPID_RALLY", "SHARP_FALL", "RAPID_DECLINE")


def anomaly_analysis_list(tag_codes=None, force=False):
    """当日个股异动列表（含异动原因）。

    tag_codes: 逗号分隔的标签，如 "SHARP_RISE,RAPID_RALLY"（大涨/快速拉升）。
    返回 item 列表，字段：thscode / analysis_content / keyword_list / tag_name。
    注意：仅当日有效，跨 0 点清空；当日数据未生成时返回空/None（错误码 3002）。
    """
    if not tag_codes:
        tag_codes = ",".join(_ANOMALY_TAGS)
    data = _request("/api/a-share/special-data/anomaly-analysis-list",
                    {"tag_codes": tag_codes}, timeout=10,
                    ttl_group="anomaly", force=force)
    if not data:
        return None
    return data.get("item") or []


def anomaly_analysis_stock(thscodes, force=False):
    """按同花顺代码批量查当日异动原因（单批上限 50 只）。

    thscodes: list[str]（如 ["688368.SH", ...]）或逗号分隔字符串。
    返回 dict[thscode] -> item（含 keyword_list / analysis_content / tag_name）；
    查不到的代码不在 dict 中。失败返回 None。
    """
    if isinstance(thscodes, (list, tuple)):
        codes = [str(c).strip() for c in thscodes if str(c).strip()]
    else:
        codes = [c.strip() for c in str(thscodes).split(",") if c.strip()]
    if not codes:
        return {}
    out: dict[str, dict] = {}
    for i in range(0, len(codes), 50):
        batch = codes[i:i + 50]
        data = _request("/api/a-share/special-data/anomaly-analysis-stock",
                        {"thscodes": ",".join(batch)}, timeout=10,
                        ttl_group="anomaly", force=force)
        if not data:
            continue
        for it in data.get("item") or []:
            t = it.get("thscode")
            if t:
                out[str(t)] = it
    return out or None


# ---------------------------------------------------------------- 交易日历

def trading_days(force=False):
    data = _request("/api/a-share/calendar/trading-days", None,
                    timeout=10, ttl_group="calendar", force=force)
    if not data:
        return None
    days = []
    for it in data.get("item") or []:
        d = it.get("date")
        if isinstance(d, str) and len(d) == 8 and d.isdigit():
            days.append(d)
    return days or None


# ---------------------------------------------------------------- 行情

def prices_snapshot(thscodes, timeout=10):
    if not thscodes:
        return None
    if isinstance(thscodes, (list, tuple)):
        codes = ",".join([str(c) for c in thscodes if c])
    else:
        codes = str(thscodes)
    data = _request("/api/a-share/prices/snapshot", {"thscodes": codes},
                    timeout=timeout, ttl_group="snapshot")
    if not data:
        return None
    out = {}
    for it in data.get("item") or []:
        t = it.get("ticker")
        if t:
            out[t] = it
    return out or None


def all_a_share_snapshot(limit=6000, offset=0, force=False):
    data = _request("/api/a-share/prices/snapshot",
                    {"limit": limit, "offset": offset},
                    timeout=20, ttl_group="market_all", force=force)
    if not data:
        return None
    out = {}
    for it in data.get("item") or []:
        t = it.get("ticker")
        if t:
            out[t] = it
    return out or None


def index_daily(index_thscode="000001.SH", start_ms=None, end_ms=None, force=False):
    """指数日 K（列表）。用于「当天是否开市」的兜底校验：
    查近几日区间时，返回的最后一根 K 线日期就是最近交易日。
    接口失败返回 None，无数据返回 []。
    """
    if not index_thscode:
        return None
    params = {"thscode": index_thscode, "interval": "1d", "adjust": "forward"}
    if start_ms is not None:
        params["start"] = start_ms
    if end_ms is not None:
        params["end"] = end_ms
    data = _request("/api/a-share-index/prices/historical", params,
                    timeout=10, ttl_group="kline", force=force)
    if not data:
        return None
    return data.get("item") or []


def stock_daily(thscode, start_ms=None, end_ms=None, adjust="forward",
                force=False, ttl_group="kline"):
    """个股日 K（单只）。接口失败返回 None，无数据返回 []。

    端点必须是 /api/a-share/prices/historical（带 a-share）。
    走成 /api/a-share-index/... 会静默返回空且 code=0，不报错——
    这是本接口最容易踩的坑，deploy/probe_volume.py 专门校验。

    返回项：date / date_ms / open / high / low / close / volume(股) / turnover(元)
    """
    if not thscode:
        return None
    params = {"thscode": thscode, "interval": "1d", "adjust": adjust}
    if start_ms is not None:
        params["start"] = start_ms
    if end_ms is not None:
        params["end"] = end_ms
    data = _request("/api/a-share/prices/historical", params, timeout=10,
                    ttl_group=ttl_group, force=force)
    if not data:
        return None
    return normalize_bars(data.get("item") or [])


def normalize_bars(items):
    """同花顺日 K item[] -> 升序标准 bar 列表。

    批量回补时传 ttl_group=None 不写全局 _CACHE：5573 条缓存会白占
    十几 MB，而 _CACHE 没有淘汰机制。
    """
    out = []
    for it in items:
        ms = it.get("date_ms")
        if ms is None:
            continue
        try:
            dt = datetime.datetime.fromtimestamp(int(ms) / 1000, _TZ_SH)
        except (TypeError, ValueError, OSError):
            continue
        out.append({
            "date": dt.strftime("%Y-%m-%d"),
            "date_ms": int(ms),
            "open": float(it.get("open_price") or 0),
            "high": float(it.get("high_price") or 0),
            "low": float(it.get("low_price") or 0),
            "close": float(it.get("close_price") or 0),
            "volume": float(it.get("volume") or 0),
            "turnover": float(it.get("turnover") or 0),
        })
    out.sort(key=lambda x: x["date"])
    return out


# ---------------------------------------------------------------- 板块 / 标的

def ths_index_list(tag="cn_concept", force=False):
    data = _request("/api/a-share-index/catalog/ths-index-list", {"tag": tag},
                    timeout=12, ttl_group="catalog", force=force)
    if not data:
        return None
    return data.get("item") or None


def ths_index_constituents(index_thscode, force=False):
    if not index_thscode:
        return None
    data = _request("/api/a-share-index/constituents/ths-stock-list",
                    {"thscode": index_thscode}, timeout=12,
                    ttl_group="catalog", force=force)
    if not data:
        return None
    return data.get("item") or None


def tickers_list(asset_type="a-share", limit=1000, offset=0, force=False):
    data = _request("/api/meta/tickers/list", {
        "asset_type": asset_type, "limit": limit, "offset": offset,
    }, timeout=12, ttl_group="catalog", force=force)
    if not data:
        return None
    return data.get("item") or None


def all_a_share_names(force=False):
    """全市场 A 股代码 -> 名称映射（分页拉全）。失败返回 None。"""
    out = {}
    offset = 0
    page_size = 1000
    while offset < 8000:
        items = tickers_list("a-share", limit=page_size, offset=offset, force=force)
        if items is None:
            return out or None
        if not items:
            break
        for it in items:
            t = it.get("ticker")
            n = it.get("name")
            if t and n:
                out[t] = n
        if len(items) < page_size:
            break
        offset += page_size
    return out or None


def to_thscode(code):
    c = str(code or "").strip()
    if not c:
        return None
    if "." in c:
        return c
    if c.startswith(("60", "68", "90", "50", "51", "58")):
        return c + ".SH"
    if c.startswith(("00", "30", "20", "15", "16", "18")):
        return c + ".SZ"
    if c.startswith(("43", "83", "87", "88", "92")):
        return c + ".BJ"
    return c + ".SZ"


# ---------------------------------------------------------------- 探活

def is_available(timeout=5):
    data = _request("/api/meta/tickers/list",
                    {"asset_type": "a-share", "limit": 1, "offset": 0},
                    timeout=timeout, retries=0, ttl_group=None)
    return bool(data)


def status():
    return {
        "configured": is_configured(),
        "last_error": dict(last_error),
        "cache_keys": len(_CACHE),
    }
