#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台股＋美股預測計分程式（只「讀取」公開行情資料，不送出任何資料）。

功能：
  1. 讀取 predictions.jsonl（每筆有 market: "TW" / "US"；舊資料缺 market 視為 TW）
  2. 補 due_date（TW 用證交所休市表；US 用 NYSE 休市規則）
  3. 補缺少的 base_close（base_date 當天收盤；非交易日則用前一個交易日；當天尚未公布則等待）
  4. 對到期且已有收盤資料的 open 預測計分（以「實際有收盤資料的交易日」往後數 N 天）
  5. 重寫 scoreboard.md（內容沒變就不寫，可重複執行）

資料來源（2026-10-29 實測）：
  TW 每日指數收盤  https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=YYYYMMDD&type=IND&response=json
       tables[0]「價格指數(臺灣證券交易所)」fields = 指數, 收盤指數, ...；非交易日或尚未公布時 data 為空陣列。
  TW 休市日       https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?response=json&queryYear=民國年
  US 標普500       FRED CSV  https://fred.stlouisfed.org/graph/fredgraph.csv?id=SP500&cosd=YYYY-MM-DD
  US 那斯達克綜合  FRED CSV  https://fred.stlouisfed.org/graph/fredgraph.csv?id=NASDAQCOM&cosd=YYYY-MM-DD
       （FRED 休市日值為 "."；資料約晚一個交易日上架）
  US 費城半導體    Nasdaq API https://api.nasdaq.com/api/quote/SOX/historical?assetclass=index&fromdate=..&todate=..&limit=..
       需瀏覽器 User-Agent 與 Accept: application/json；data.tradesTable.rows[].date="MM/DD/YYYY", close="11,482.50"
  交叉比對（--crosscheck，只印出不寫檔）：Yahoo Finance chart API（^GSPC/^IXIC/^SOX）與 Nasdaq API（COMP）。

用法：
  python3 score.py               # 正常執行（冪等，可重複跑）
  python3 score.py --offline     # 不連網，只用快取
  python3 score.py --crosscheck  # 另外印出美股收盤的雙來源比對
"""
import csv
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")
ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
PRED = os.path.join(HERE, "predictions.jsonl")
BOARD = os.path.join(HERE, "scoreboard.md")
CACHE = os.path.join(HERE, "cache")
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
UA = {"User-Agent": "Mozilla/5.0 (tw-stock-tracker; personal use)"}
MI_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={d}&type=IND&response=json"
HOL_URL = "https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?response=json&queryYear={y}"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}"
NASDAQ_URL = ("https://api.nasdaq.com/api/quote/{sym}/historical?assetclass=index"
              "&fromdate={start}&todate={end}&limit=400")
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1mo&interval=1d"

TAIEX_TARGET = "加權指數"                 # 預測檔中的名稱
TAIEX_TWSE_NAME = "發行量加權股價指數"     # MI_INDEX 內的名稱
PUBLISH_HOUR = 15                          # 台北時間；當日 17:00 前不把「空資料」視為休市
# 美股標的：預測檔名稱 → (主要來源, 代碼, Yahoo 代碼)
US_TARGETS = {
    "標普500指數": ("FRED", "SP500", "^GSPC"),
    "那斯達克綜合指數": ("FRED", "NASDAQCOM", "^IXIC"),
    "費城半導體指數": ("NASDAQ", "SOX", "^SOX"),
}
US_REFETCH_MIN = 60                        # 美股快取至少隔 60 分鐘才重新抓
CLOSED = "CLOSED"                          # 非交易日標記
OFFLINE = "--offline" in sys.argv
_last_req = {}


def now():
    return dt.datetime.now(TZ)


def today():
    return now().date()


def today_et():
    return dt.datetime.now(ET).date()


def d2s(d):
    return d.isoformat()


def s2d(s):
    return dt.date.fromisoformat(s)


def market_of(p):
    return p.get("market") or "TW"


def http_get(url, headers, host_gap=2.5):
    if OFFLINE:
        raise RuntimeError("offline 模式，無快取資料：" + url)
    host = url.split("/")[2]
    wait = host_gap - (time.time() - _last_req.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=40) as r:
                _last_req[host] = time.time()
                return r.read().decode("utf-8")
        except Exception as e:  # noqa
            last_err = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"抓取失敗 {url}: {last_err}")


def http_json(url, headers=UA):
    return json.loads(http_get(url, headers))


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


# =====================================================================
# 台股（TWSE）
# =====================================================================
_hol = {}


def tw_holidays(year):
    """回傳 (該年非交易日集合（不含週末）, 是否取得官方表)。名稱含『開始交易』『最後交易』者是交易日。"""
    if year in _hol:
        return _hol[year]
    path = os.path.join(CACHE, f"holidays_{year}.json")
    data = None
    if os.path.exists(path):
        data = json.load(open(path, encoding="utf-8"))
    else:
        try:
            data = http_json(HOL_URL.format(y=year - 1911))
            if data.get("data"):
                write_json(path, data)
            else:
                data = None
        except Exception as e:
            print(f"[警告] 無法取得 {year} 台股休市日：{e}", file=sys.stderr)
    s = set()
    for row in (data or {}).get("data", []):
        name = row[1]
        if "開始交易" in name or "最後交易" in name:
            continue
        s.add(row[0])
    _hol[year] = (s, data is not None)
    return _hol[year]


_mem = {}


def tw_day_closes(d):
    """回傳 {指數名稱: 收盤}；非交易日回傳 {}；尚未公布回傳 None。同一次執行內不重複抓。"""
    if d not in _mem:
        _mem[d] = _tw_day_closes(d)
    return _mem[d]


def _tw_day_closes(d):
    key = d.strftime("%Y%m%d")
    path = os.path.join(CACHE, f"MI_INDEX_IND_{key}.json")
    if os.path.exists(path):
        raw = json.load(open(path, encoding="utf-8"))
    else:
        if d > today():
            return None
        raw = http_json(MI_URL.format(d=key))
        rows = (raw.get("tables") or [{}])[0].get("data") or []
        window_passed = d < today() or (d == today() and now().hour >= PUBLISH_HOUR + 2)
        if rows or window_passed:
            write_json(path, raw)
        else:
            return None  # 當天還沒公布，不快取
    out = {}
    for t in raw.get("tables") or []:
        for row in t.get("data") or []:
            try:
                out.setdefault(row[0], float(str(row[1]).replace(",", "")))
            except (ValueError, IndexError):
                pass
    return out


def twse_name(target):
    return TAIEX_TWSE_NAME if target == TAIEX_TARGET else target


# =====================================================================
# 美股（NYSE 休市規則 + FRED / Nasdaq API）
# =====================================================================
def _easter(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = (h + l_ - 7 * m + 114) % 31 + 1
    return dt.date(y, month, day)


def _nth_weekday(y, month, weekday, n):
    d = dt.date(y, month, 1)
    d += dt.timedelta(days=(weekday - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def _last_weekday(y, month, weekday):
    d = dt.date(y, month + 1, 1) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d):
    if d.weekday() == 5:
        return d - dt.timedelta(days=1)
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    return d


def nyse_holidays(y):
    """NYSE 全日休市（依 NYSE 規則計算；不含臨時休市，臨時休市由『實際有無收盤資料』處理）。"""
    h = {}
    ny = dt.date(y, 1, 1)
    if ny.weekday() != 5:              # 元旦逢週六不補假（NYSE 規則）
        h[_observed(ny)] = "元旦"
    h[_nth_weekday(y, 1, 0, 3)] = "馬丁路德金恩紀念日"
    h[_nth_weekday(y, 2, 0, 3)] = "總統日"
    h[_easter(y) - dt.timedelta(days=2)] = "耶穌受難日"
    h[_last_weekday(y, 5, 0)] = "陣亡將士紀念日"
    if y >= 2022:
        h[_observed(dt.date(y, 6, 19))] = "六月節"
    h[_observed(dt.date(y, 7, 4))] = "獨立紀念日"
    h[_nth_weekday(y, 9, 0, 1)] = "勞動節"
    h[_nth_weekday(y, 11, 3, 4)] = "感恩節"
    h[_observed(dt.date(y, 12, 25))] = "聖誕節"
    return h


def nyse_early_closes(y):
    """NYSE 提早收盤（13:00 ET）。仍算交易日，只做標註。"""
    out = {_nth_weekday(y, 11, 3, 4) + dt.timedelta(days=1): "感恩節隔天"}
    xmas_eve = dt.date(y, 12, 24)
    if xmas_eve.weekday() < 5:
        out[xmas_eve] = "平安夜"
    jul3 = dt.date(y, 7, 3)
    if jul3.weekday() < 4:            # 7/3 為週一～週四（7/4 非週六）時提早收盤
        out[jul3] = "獨立紀念日前一天"
    return out


_us_series = {}
_us_failed = set()                         # 本次執行抓取失敗過的代碼，不再重試（避免卡很久）


def _us_cache_path(code):
    return os.path.join(CACHE, f"US_{code}.json")


def _us_load(code):
    if code not in _us_series:
        path = _us_cache_path(code)
        _us_series[code] = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else \
            {"closes": {}, "last_date": None, "fetched_at": None, "source": None}
    return _us_series[code]


def _fetch_fred(sid, start):
    url = FRED_URL.format(sid=sid, start=d2s(start))
    # 注意：FRED 對「瀏覽器 UA＋非瀏覽器 TLS」的請求會卡住不回應；用 urllib 預設 UA 即可（實測 <0.1 秒）
    text = http_get(url, {})
    out = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2 or not row[0][:4].isdigit():
            continue
        if row[1].strip() in ("", "."):
            out[row[0]] = None        # FRED 休市日
            continue
        out[row[0]] = float(row[1])
    return url, out


def _fetch_nasdaq(sym, start, end):
    url = NASDAQ_URL.format(sym=sym, start=d2s(start), end=d2s(end))
    raw = json.loads(http_get(url, {"User-Agent": BROWSER_UA, "Accept": "application/json"}))
    rows = (((raw.get("data") or {}).get("tradesTable") or {}).get("rows")) or []
    if not rows:
        raise RuntimeError(f"Nasdaq API 無資料：{raw.get('status')}")
    out = {}
    for r in rows:
        m, d, y = r["date"].split("/")
        out[f"{y}-{m}-{d}"] = float(r["close"].replace(",", ""))
    return url, out


def _us_refresh(code_src, code, need_from):
    s = _us_load(code)
    if s.get("fetched_at"):
        age = (now() - dt.datetime.fromisoformat(s["fetched_at"])).total_seconds() / 60
        if age < US_REFETCH_MIN and need_from >= s.get("cache_from", "9999"):
            return False
    start = min(s2d(need_from), s2d(s["cache_from"]) if s.get("cache_from") else s2d(need_from)) - dt.timedelta(days=10)
    if code_src == "FRED":
        url, got = _fetch_fred(code, start)
    else:
        url, got = _fetch_nasdaq(code, start, today_et())
    closes = s["closes"]
    for k, v in got.items():
        if v is not None:
            closes[k] = v
    s["closes"] = dict(sorted(closes.items()))
    real = [k for k, v in s["closes"].items()]
    s["last_date"] = max(real) if real else None
    s["cache_from"] = min(d2s(start), s.get("cache_from") or "9999")
    s["fetched_at"] = now().isoformat(timespec="seconds")
    s["source"] = url
    write_json(_us_cache_path(code), s)
    return True


def us_close(target, d):
    """回傳 float 收盤；非交易日回傳 CLOSED；尚未公布（或來源還沒上架）回傳 None。"""
    if target not in US_TARGETS:
        raise KeyError(f"未知的美股標的：{target}（可用：{list(US_TARGETS)}）")
    src, code, _ = US_TARGETS[target]
    if d.weekday() >= 5 or d in nyse_holidays(d.year):
        return CLOSED
    now_et = dt.datetime.now(ET)
    if d > now_et.date() or (d == now_et.date() and now_et.hour < 18):
        return None                     # 美東 18:00 前視為當天尚未收盤／未公布
    s = _us_load(code)
    key = d2s(d)
    if key not in s["closes"] and (s["last_date"] is None or key > s["last_date"]
                                   or key < s.get("cache_from", "0000")):
        if not OFFLINE and code not in _us_failed:
            try:
                _us_refresh(src, code, key)
            except Exception as e:
                _us_failed.add(code)
                print(f"[警告] {target} 抓取失敗（本次不再重試）：{e}", file=sys.stderr)
        s = _us_load(code)
    if key in s["closes"]:
        return s["closes"][key]
    if s["last_date"] and key < s["last_date"] and key >= s.get("cache_from", "9999"):
        return CLOSED                   # 來源已有更晚日期卻沒有這天 → 臨時休市
    return None


# =====================================================================
# 市場共用：交易日、收盤
# =====================================================================
def is_trading_day_by_calendar(market, d):
    """回傳 (是否交易日, 行事曆是否可靠)。"""
    if d.weekday() >= 5:
        return False, True
    if market == "US":
        return d not in nyse_holidays(d.year), True
    s, ok = tw_holidays(d.year)
    return d2s(d) not in s, ok


def add_trading_days(market, base, n):
    """依行事曆往後數 n 個交易日。回傳 (日期, 是否為估計)。"""
    d, cnt, est = base, 0, False
    while cnt < n:
        d += dt.timedelta(days=1)
        trading, ok = is_trading_day_by_calendar(market, d)
        est = est or not ok
        if trading:
            cnt += 1
    return d, est


def names_of(p):
    if market_of(p) == "US":
        return [p["target"]]
    names = [twse_name(p["target"])]
    if p["kind"] == "relative":
        names.append(TAIEX_TWSE_NAME)
    return names


def closes_on(market, d, names):
    """回傳 {name: close}；非交易日回傳 {}；尚未公布回傳 None。"""
    if market == "US":
        out = {}
        for n in names:
            v = us_close(n, d)
            if v is None:
                return None
            if v == CLOSED:
                return {}
            out[n] = v
        return out
    c = tw_day_closes(d)
    if not c:
        return c
    missing = [n for n in names if n not in c]
    if missing:
        raise KeyError(f"{d2s(d)} 證交所資料中找不到指數名稱：{missing}")
    return {n: c[n] for n in names}


def close_on_or_before(market, d, names, max_back=15):
    """回傳 (實際日期, {name: close})；若 d 當天尚未公布則回傳 (None, None)。"""
    first = True
    for i in range(max_back):
        day = d - dt.timedelta(days=i)
        if day.weekday() >= 5:
            continue
        c = closes_on(market, day, names)
        if c is None:
            if first:
                return None, None
            continue
        first = False
        if c:
            return day, c
    return None, None


def nth_trading_day_after(market, base, n, names):
    """以實際有收盤資料的日子往後數 n 天（處理颱風假等臨時休市）。未到／未公布回傳 None。"""
    d, cnt = base, 0
    while cnt < n:
        d += dt.timedelta(days=1)
        if d > today():
            return None
        trading, _ = is_trading_day_by_calendar(market, d)
        if not trading:
            continue
        c = closes_on(market, d, names)
        if c is None:
            return None
        if c:
            cnt += 1
    return d


# =====================================================================
# 主流程
# =====================================================================
def load():
    preds = []
    with open(PRED, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if line:
                try:
                    preds.append(json.loads(line))
                except json.JSONDecodeError as e:
                    sys.exit(f"predictions.jsonl 第 {ln} 行 JSON 錯誤：{e}")
    return preds


def save(preds):
    text = "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in preds)
    old = open(PRED, encoding="utf-8").read() if os.path.exists(PRED) else ""
    if text != old:
        tmp = PRED + ".tmp"
        open(tmp, "w", encoding="utf-8").write(text)
        os.replace(tmp, PRED)
        return True
    return False


def check_rules(preds):
    seen = defaultdict(list)
    for p in preds:
        if p.get("status") == "open":
            seen[(market_of(p), p["target"], p["horizon_trading_days"])].append(p["id"])
        if market_of(p) == "US":
            if p["kind"] != "direction":
                print(f"[規則違反] {p['id']} 美股只支援 kind=direction", file=sys.stderr)
            if p["target"] not in US_TARGETS:
                print(f"[規則違反] {p['id']} 未知美股標的 {p['target']}", file=sys.stderr)
    for k, ids in seen.items():
        if len(ids) > 1:
            print(f"[規則違反] {k[0]} {k[1]} / {k[2]} 日 同時有多筆 open 預測：{ids}", file=sys.stderr)
    ids = [p["id"] for p in preds]
    if len(ids) != len(set(ids)):
        print("[規則違反] id 重複", file=sys.stderr)


def fill_base(p):
    rel = p["kind"] == "relative"
    if p.get("base_close") is not None and (not rel or p.get("base_taiex_close") is not None):
        return False
    names = names_of(p)
    day, c = close_on_or_before(market_of(p), s2d(p["base_date"]), names)
    if day is None:
        print(f"[等待] {p['id']} 基準日 {p['base_date']}（{market_of(p)}）收盤尚未公布，base_close 維持 null")
        return False
    p["base_close"] = c[names[0]]
    if rel:
        p["base_taiex_close"] = c[TAIEX_TWSE_NAME]
    p["base_close_date"] = d2s(day)
    print(f"[補基準] {p['id']} base_close={p['base_close']}（{d2s(day)}）")
    return True


def ensure_due(p):
    if p.get("due_date"):
        return False
    due, est = add_trading_days(market_of(p), s2d(p["base_date"]), int(p["horizon_trading_days"]))
    p["due_date"], p["due_date_estimated"] = d2s(due), est
    return True


def score(p):
    if p.get("status") != "open" or p.get("base_close") is None:
        return False
    if s2d(p["due_date"]) > today():
        return False
    market, names = market_of(p), names_of(p)
    base = s2d(p.get("base_close_date") or p["base_date"])
    actual = nth_trading_day_after(market, base, int(p["horizon_trading_days"]), names)
    if actual is None:
        return False
    c = closes_on(market, actual, names)
    ret = c[names[0]] / p["base_close"] - 1
    p["result_date"] = d2s(actual)
    p["result_close"] = c[names[0]]
    p["result_return_pct"] = round(ret * 100, 2)
    if p["kind"] == "direction":
        hit = (ret > 0 and p["call"] == "漲") or (ret < 0 and p["call"] == "跌")
    else:
        tret = c[TAIEX_TWSE_NAME] / p["base_taiex_close"] - 1
        p["result_taiex_close"] = c[TAIEX_TWSE_NAME]
        p["result_taiex_return_pct"] = round(tret * 100, 2)
        excess = ret - tret
        p["result_excess_pct"] = round(excess * 100, 2)
        hit = (excess > 0 and p["call"] == "強於大盤") or (excess < 0 and p["call"] == "弱於大盤")
    if d2s(actual) != p["due_date"]:
        p["due_date_note"] = f"原 due_date {p['due_date']}，依實際交易日改為 {d2s(actual)}"
        p["due_date"] = d2s(actual)
    p["status"] = "hit" if hit else "miss"
    p["scored_on"] = d2s(today())
    print(f"[計分] {p['id']} → {p['status']}")
    return True


def shows_of(p):
    out = [s["show"] for s in p.get("sources", []) if isinstance(s, dict) and s.get("show")]
    return sorted(set(out)) or ["（未標來源）"]


MARKET_NAME = {"TW": "台股", "US": "美股"}


def rate(rows):
    done = [p for p in rows if p["status"] in ("hit", "miss")]
    h = sum(p["status"] == "hit" for p in done)
    pct = f"{h / len(done) * 100:.1f}%" if done else "—"
    return f"| {h} | {len(done) - h} | {len(rows) - len(done)} | {pct} |"


def board(preds):
    L = ["# 台股＋美股預測計分板", "",
         f"> 由 `score.py` 自動產生，請勿手改。產生日期：{d2s(today())}（台北時間）",
         "> 資料：台股＝臺灣證券交易所 MI_INDEX 每日收盤；美股＝FRED（標普500、那斯達克綜合）、Nasdaq 官方 API（費城半導體）每日收盤。",
         "> 規則：方向題以「到期日收盤 vs 基準收盤」判斷漲跌；相對題以「類股報酬 − 加權指數報酬」判斷強弱；剛好持平算未命中。計分只看原始 call，revisions 不改變計分。美股日期為美東交易日。",
         ""]
    hdr = ["| 分組 | 命中 | 未中 | 未到期 | 命中率 |", "|---|---|---|---|---|"]
    L += ["## 總體", ""] + hdr + [f"| 全部 {rate(preds)}"]
    groups = [("依市場", lambda p: [MARKET_NAME.get(market_of(p), market_of(p))]),
              ("依標的", lambda p: [p["target"]]),
              ("依期間", lambda p: [f"{p['horizon_trading_days']} 個交易日"]),
              ("依市場×期間", lambda p: [f"{MARKET_NAME.get(market_of(p))} {p['horizon_trading_days']} 日"]),
              ("依信心", lambda p: [p["confidence"]]),
              ("依來源節目", shows_of)]
    for title, fn in groups:
        g = defaultdict(list)
        for p in preds:
            for k in fn(p):
                g[k].append(p)
        L += ["", f"## {title}", ""] + hdr + [f"| {k} {rate(v)}" for k, v in sorted(g.items())]
    L += ["", "## 未到期預測", "",
          "| id | 市場 | 標的 | 類型 | 期間 | 預測 | 信心 | 基準日 | 基準收盤 | 到期日 |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for p in preds:
        if p["status"] == "open":
            bc = "待補" if p.get("base_close") is None else f"{p['base_close']:,.2f}"
            due = p["due_date"] + ("（估）" if p.get("due_date_estimated") else "")
            if market_of(p) == "US" and s2d(p["due_date"]) in nyse_early_closes(s2d(p["due_date"]).year):
                due += "（美股提早收盤日）"
            L.append(f"| {p['id']} | {MARKET_NAME.get(market_of(p))} | {p['target']} | {p['kind']} | "
                     f"{p['horizon_trading_days']} | {p['call']} | {p['confidence']} | {p['base_date']} | {bc} | {due} |")
    done = [p for p in preds if p["status"] != "open"]
    if done:
        L += ["", "## 已計分", "",
              "| id | 市場 | 標的 | 預測 | 結果 | 標的報酬% | 大盤報酬% | 超額% | 到期日 |",
              "|---|---|---|---|---|---|---|---|---|"]
        for p in done:
            L.append(f"| {p['id']} | {MARKET_NAME.get(market_of(p))} | {p['target']} | {p['call']} | "
                     f"{'✅ 命中' if p['status'] == 'hit' else '❌ 未中'} | {p.get('result_return_pct')} | "
                     f"{p.get('result_taiex_return_pct', '—')} | {p.get('result_excess_pct', '—')} | {p['due_date']} |")
    text = "\n".join(L) + "\n"
    old = open(BOARD, encoding="utf-8").read() if os.path.exists(BOARD) else ""
    if text != old:
        open(BOARD, "w", encoding="utf-8").write(text)
        return True
    return False


def crosscheck():
    """美股雙來源比對：主要來源 vs Yahoo Finance（另加 FRED vs Nasdaq API 的那斯達克綜合）。只印出。"""
    print("== 美股收盤雙來源比對 ==")
    for target, (src, code, ysym) in US_TARGETS.items():
        s = _us_load(code)
        try:
            raw = json.loads(http_get(YAHOO_URL.format(sym=urllib.request.quote(ysym)), {"User-Agent": BROWSER_UA}))
            r = raw["chart"]["result"][0]
            y = {dt.datetime.fromtimestamp(t, ET).date().isoformat(): c
                 for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]) if c is not None}
        except Exception as e:
            print(f"{target}: Yahoo 取得失敗 {e}")
            continue
        common = sorted(set(s["closes"]) & set(y))
        if not common:
            print(f"{target}: 無共同日期（先執行一次 score.py 建快取）")
            continue
        k = common[-1]
        print(f"{target} {k}: {src}={s['closes'][k]:,.2f}  Yahoo={y[k]:,.2f}  差={s['closes'][k] - y[k]:+.2f}")
    try:
        _, nq = _fetch_nasdaq("COMP", today_et() - dt.timedelta(days=15), today_et())
        f = _us_load("NASDAQCOM")["closes"]
        common = sorted(set(f) & set(nq))
        if common:
            k = common[-1]
            print(f"那斯達克綜合指數 {k}: FRED={f[k]:,.2f}  Nasdaq API={nq[k]:,.2f}  差={f[k] - nq[k]:+.2f}")
    except Exception as e:
        print(f"Nasdaq API COMP 取得失敗 {e}")


def main():
    os.makedirs(CACHE, exist_ok=True)
    preds = load()
    check_rules(preds)
    changed = False
    for p in preds:
        if "market" not in p:
            p["market"] = "TW"
            changed = True
        p.setdefault("status", "open")
        p.setdefault("revisions", [])
        try:
            changed |= ensure_due(p)
            changed |= fill_base(p)
            changed |= score(p)
        except Exception as e:
            print(f"[錯誤] {p.get('id')}: {e}", file=sys.stderr)
    wrote = save(preds) if changed else False
    b = board(preds)
    print(f"predictions.jsonl {'已更新' if wrote else '無變更'}；scoreboard.md {'已更新' if b else '無變更'}")
    if "--crosscheck" in sys.argv and not OFFLINE:
        for target in US_TARGETS:     # 確保快取有近期資料
            us_close(target, today_et() - dt.timedelta(days=1))
        crosscheck()


if __name__ == "__main__":
    main()
