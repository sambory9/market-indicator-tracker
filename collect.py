"""지표추적자 데이터 수집기.

평일 장 마감 후 GitHub Actions에서 실행된다.
- 야후 파이낸스(yfinance): 지수·종목·환율·원자재·코인
- FRED: 미국채 2년·10년 (키 불필요)
- 한국은행 ECOS: 국고채 3년·10년 (환경변수 ECOS_API_KEY)
- pykrx: 코스피·코스닥 주체별 순매수

결과:
- data/prices/<id>.csv   : 지표별 일간 시계열 (date, open, high, low, close)
- data/flows/<market>.csv: 주체별 순매수 (억원)
- data/latest.json       : 지표별 최신값과 계산된 신호 (리포트용)
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PRICES = DATA / "prices"
FLOWS = DATA / "flows"

# (id, 이름, 섹터, 야후 티커)
YAHOO = [
    ("kospi", "코스피", "지수", "^KS11"),
    ("kosdaq", "코스닥", "지수", "^KQ11"),
    ("sp500", "S&P 500", "지수", "^GSPC"),
    ("nasdaq", "나스닥", "지수", "^IXIC"),
    ("dow", "다우", "지수", "^DJI"),
    ("russell2000", "러셀 2000", "지수", "^RUT"),
    ("nikkei", "니케이225", "지수", "^N225"),
    ("hscei", "H지수(HSCEI)", "지수", "^HSCE"),
    ("nq_fut", "나스닥100 선물", "선물", "NQ=F"),
    ("es_fut", "S&P500 선물", "선물", "ES=F"),
    ("samsung_elec", "삼성전자", "종목", "005930.KS"),
    ("sk_hynix", "SK하이닉스", "종목", "000660.KS"),
    ("nvidia", "엔비디아", "종목", "NVDA"),
    ("oracle", "오라클", "종목", "ORCL"),
    ("usdkrw", "원/달러", "환율", "KRW=X"),
    ("usdjpy", "달러/엔", "환율", "JPY=X"),
    ("dxy", "달러지수", "환율", "DX-Y.NYB"),
    ("gold", "금", "원자재/코인", "GC=F"),
    ("wti", "WTI", "원자재/코인", "CL=F"),
    ("btc", "비트코인", "원자재/코인", "BTC-USD"),
    ("vix", "VIX", "지수", "^VIX"),
]

FRED = [
    ("ust2y", "미국채 2년", "금리", "DGS2"),
    ("ust10y", "미국채 10년", "금리", "DGS10"),
]

ECOS = [
    ("ktb3y", "국고채 3년", "금리", "010200000"),
    ("ktb10y", "국고채 10년", "금리", "010210000"),
]

FLOW_MARKETS = [("kospi_flow", "코스피 수급", "KOSPI"), ("kosdaq_flow", "코스닥 수급", "KOSDAQ")]
FLOW_COLS = ["개인", "외국인", "기관합계", "연기금", "투신"]

# 추적 중 MACD·RSI를 함께 보는 지표 (방송 구성 기준)
OSC_IDS = {"gold", "wti", "btc"}


# ---------------------------------------------------------------- 수집

def fetch_yahoo(ticker: str, years: int = 2) -> pd.DataFrame:
    import yfinance as yf

    for attempt in range(3):
        try:
            df = yf.download(ticker, period=f"{years}y", interval="1d",
                             auto_adjust=False, progress=False, threads=False)
            if df is not None and not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df = df.rename(columns=str.lower)[["open", "high", "low", "close"]]
                df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
                df.index.name = "date"
                return df.dropna(subset=["close"])
        except Exception as e:  # noqa: BLE001
            print(f"  yahoo {ticker} 시도 {attempt + 1} 실패: {e}", file=sys.stderr)
        time.sleep(3)
    raise RuntimeError(f"yahoo {ticker} 수집 실패")


def fetch_fred(series: str) -> pd.DataFrame:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = ["date", "close"]
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna().set_index("date")
    return df[df.index >= df.index.max() - pd.Timedelta(days=365 * 3)]


def fetch_ecos(item: str, key: str) -> pd.DataFrame:
    end = datetime.now(KST)
    start = end - timedelta(days=365 * 3)
    url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/2000/"
           f"817Y002/D/{start:%Y%m%d}/{end:%Y%m%d}/{item}")
    body, last = None, None
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            body = r.json()
            break
        except Exception as e:  # noqa: BLE001
            # 인증키가 URL에 들어 있으므로 원문 오류를 그대로 남기지 않는다
            last = type(e).__name__
            time.sleep(10)
    if body is None:
        raise RuntimeError(f"ECOS 연결 실패({last})")
    rows = body.get("StatisticSearch", {}).get("row", [])
    if not rows:
        raise RuntimeError(f"ECOS 응답 없음: {body.get('RESULT', body)}")
    df = pd.DataFrame({"date": pd.to_datetime([x["TIME"] for x in rows], format="%Y%m%d"),
                       "close": pd.to_numeric([x["DATA_VALUE"] for x in rows], errors="coerce")})
    return df.dropna().set_index("date")


NAVER_SOSOK = {"KOSPI": ["01", ""], "KOSDAQ": ["02"]}
NAVER_COLMAP = {"개인": "개인", "외국인": "외국인", "기관계": "기관합계",
                "투신(사모)": "투신", "투신": "투신", "연기금등": "연기금", "연기금": "연기금"}


def fetch_flow_naver(market: str, pages: int = 15) -> pd.DataFrame:
    """네이버 금융 '투자자별 매매동향(일별)'. 단위 억원."""
    hdr = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/sise/"}
    bizdate = datetime.now(KST).strftime("%Y%m%d")
    last_err = None
    for sosok in NAVER_SOSOK[market]:
        frames = []
        try:
            for page in range(1, pages + 1):
                url = (f"https://finance.naver.com/sise/investorDealTrendDay.naver"
                       f"?bizdate={bizdate}&sosok={sosok}&page={page}")
                res = requests.get(url, headers=hdr, timeout=20)
                res.encoding = "euc-kr"
                try:
                    tables = pd.read_html(io.StringIO(res.text))
                except Exception as e:  # noqa: BLE001
                    tables, _ = [], e
                t = next((x for x in tables if x.shape[1] >= 8), None)
                if page == 1:
                    (FLOWS / f"_debug_{market}_{sosok or 'blank'}.txt").write_text(
                        f"status={res.status_code} len={len(res.text)} url={res.url}\n"
                        f"tables={[x.shape for x in tables]}\n"
                        f"cols={[list(map(str, x.columns))[:12] for x in tables if x.shape[1] >= 8]}\n"
                        f"head={[x.head(4).to_dict('records') for x in tables if x.shape[1] >= 8]}\n"
                        f"---\n{res.text[:2500]}\n", encoding="utf-8")
                if t is None:
                    break
                if isinstance(t.columns, pd.MultiIndex):
                    t.columns = [c[-1] for c in t.columns]
                t = t.rename(columns={t.columns[0]: "date"})
                t = t[t["date"].astype(str).str.match(r"^\d{2}\.\d{2}\.\d{2}$")]
                if t.empty:
                    break
                frames.append(t)
                time.sleep(0.3)
            if not frames:
                raise RuntimeError(f"sosok={sosok} 표 없음")
            df = pd.concat(frames)
            df["date"] = pd.to_datetime(df["date"], format="%y.%m.%d")
            df = df.set_index("date").rename(columns=NAVER_COLMAP)
            out = pd.DataFrame(index=df.index)
            for c in FLOW_COLS:
                out[c] = pd.to_numeric(df[c].astype(str).str.replace(",", ""), errors="coerce") if c in df.columns else np.nan
            out = out[~out.index.duplicated()].sort_index()
            out.index.name = "date"
            (FLOWS / f"_source_{market}.txt").write_text(
                f"naver sosok={sosok!r} rows={len(out)} cols={list(df.columns)}\n", encoding="utf-8")
            return out
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"naver 수급 실패: {last_err}")


def fetch_flow_daum(market: str, pages: int = 3, per_page: int = 100) -> pd.DataFrame:
    """다음 금융 투자자별 일별 순매수 (원 → 억원). 본장 기준."""
    hdr = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
           "Referer": "https://finance.daum.net/domestic/investors",
           "Accept": "application/json, text/plain, */*"}
    rows = []
    for page in range(1, pages + 1):
        url = (f"https://finance.daum.net/api/investor/{market}/days?market={market}"
               f"&page={page}&perPage={per_page}&details=true&pagination=true")
        res = requests.get(url, headers=hdr, timeout=20)
        res.raise_for_status()
        data = res.json().get("data", [])
        if not data:
            break
        rows.extend(data)
        time.sleep(0.3)
    if not rows:
        raise RuntimeError("daum 응답 없음")
    recs = []
    for x in rows:
        d = x.get("details") or {}
        recs.append({
            "date": pd.to_datetime(x["date"][:10]),
            "개인": x.get("individualStraightPurchasePrice"),
            "외국인": x.get("foreignStraightPurchasePrice"),
            "기관합계": x.get("institutionStraightPurchasePrice"),
            "연기금": d.get("PENSION_FUND"),
            "투신": d.get("MUTUAL_FUND"),
        })
    out = pd.DataFrame(recs).set_index("date").sort_index()
    out = out[~out.index.duplicated(keep="last")]
    out = (out.apply(pd.to_numeric, errors="coerce") / 1e8).round(0)
    return out[FLOW_COLS]


def fetch_flow(market: str) -> pd.DataFrame:
    try:
        return fetch_flow_daum(market)
    except Exception as e:  # noqa: BLE001
        print(f"  daum {market} 실패({e}) → pykrx로 대체", file=sys.stderr)
        return fetch_flow_pykrx(market)


def fetch_flow_pykrx(market: str) -> pd.DataFrame:
    from pykrx import stock

    end = datetime.now(KST)
    start = end - timedelta(days=400)
    df = stock.get_market_trading_value_by_date(f"{start:%Y%m%d}", f"{end:%Y%m%d}",
                                                market, detail=True)
    if df is None or df.empty:
        raise RuntimeError("pykrx 응답 없음")
    df = df.rename(columns={"외국인": "외국인", "외국인합계": "외국인"})
    if "기관합계" not in df.columns:
        inst = [c for c in ["금융투자", "보험", "투신", "사모", "은행", "기타금융", "연기금"] if c in df.columns]
        df["기관합계"] = df[inst].sum(axis=1)
    out = pd.DataFrame(index=pd.to_datetime(df.index))
    for c in FLOW_COLS:
        out[c] = (df[c] / 1e8).round(0) if c in df.columns else np.nan  # 원 → 억원
    out.index.name = "date"
    return out


# ---------------------------------------------------------------- 계산

def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def macd(close: pd.Series):
    line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    sig = line.ewm(span=9, adjust=False).mean()
    return line, sig, line - sig


def pct(a, b):
    return None if b in (None, 0) or pd.isna(b) or pd.isna(a) else round((a / b - 1) * 100, 2)


def r(x, n=2):
    return None if x is None or pd.isna(x) else round(float(x), n)


def summarize_price(id_: str, name: str, sector: str, df: pd.DataFrame, is_rate=False) -> dict:
    c = df["close"].dropna()
    last = c.iloc[-1]
    out = {"id": id_, "name": name, "sector": sector, "date": c.index[-1].strftime("%Y-%m-%d"),
           "close": r(last, 4 if id_ in ("usdjpy",) else 2)}

    def back(n):
        return c.iloc[-1 - n] if len(c) > n else None

    if is_rate:  # 금리: bp 변화
        for k, n in (("chg_1d_bp", 1), ("chg_5d_bp", 5), ("chg_20d_bp", 20)):
            b = back(n)
            out[k] = None if b is None else r((last - b) * 100, 1)
    else:
        for k, n in (("chg_1d", 1), ("chg_5d", 5), ("chg_20d", 20)):
            out[k] = pct(last, back(n))

    signals = []
    for n in (20, 60, 120):
        if len(c) >= n:
            ma = c.rolling(n).mean()
            out[f"ma{n}"] = r(ma.iloc[-1])
            above, prev_above = last > ma.iloc[-1], c.iloc[-2] > ma.iloc[-2]
            out[f"above_ma{n}"] = bool(above)
            if above != prev_above:
                signals.append(f"{n}일선 {'상향 돌파' if above else '하향 이탈'}")

    if len(c) >= 252:
        hi, lo = c.iloc[-252:].max(), c.iloc[-252:].min()
        out["from_52w_high"] = pct(last, hi)
        if last >= hi:
            signals.append("52주 신고가")
        elif last <= lo:
            signals.append("52주 신저가")

    if not is_rate:
        rv = rsi(c)
        out["rsi14"] = r(rv.iloc[-1], 1)
        if rv.iloc[-1] >= 70:
            signals.append(f"RSI 과열({out['rsi14']})")
        elif rv.iloc[-1] <= 30:
            signals.append(f"RSI 과매도({out['rsi14']})")
        if id_ in OSC_IDS:
            line, sig, hist = macd(c)
            out["macd"], out["macd_signal"], out["macd_hist"] = r(line.iloc[-1], 3), r(sig.iloc[-1], 3), r(hist.iloc[-1], 3)
            if len(hist) > 1 and np.sign(hist.iloc[-1]) != np.sign(hist.iloc[-2]):
                signals.append("MACD 골든크로스" if hist.iloc[-1] > 0 else "MACD 데드크로스")

    # 볼린저밴드(20, 2σ): 방송에서 '밴드 하단을 타고/끌고 내려간다'로 자주 언급
    if len(c) >= 20:
        mid, sd = c.rolling(20).mean(), c.rolling(20).std()
        up_b, lo_b = mid + 2 * sd, mid - 2 * sd
        out["bb_upper"], out["bb_lower"] = r(up_b.iloc[-1]), r(lo_b.iloc[-1])
        width = up_b.iloc[-1] - lo_b.iloc[-1]
        out["bb_pos"] = r((last - lo_b.iloc[-1]) / width * 100, 0) if width else None  # 0=하단, 100=상단
        if last < lo_b.iloc[-1]:
            signals.append("볼린저 하단 이탈")
        elif last > up_b.iloc[-1]:
            signals.append("볼린저 상단 돌파")

    # 최근 박스권: 직전 5·10영업일 고가·저가 (오늘 제외)
    hi_s = df["high"] if "high" in df and df["high"].notna().any() else c
    lo_s = df["low"] if "low" in df and df["low"].notna().any() else c
    for n in (5, 10):
        if len(c) > n:
            h, l = hi_s.iloc[-1 - n:-1].max(), lo_s.iloc[-1 - n:-1].min()
            out[f"range{n}_high"], out[f"range{n}_low"] = r(h), r(l)
    if out.get("range5_low") is not None:
        if last < out["range5_low"]:
            signals.append(f"최근 5일 박스 하단({out['range5_low']}) 이탈")
        elif last > out["range5_high"]:
            signals.append(f"최근 5일 박스 상단({out['range5_high']}) 돌파")

    # 주봉: 주간 등락, 20·60·120주선 위치 (일요일 주간판용)
    w = c.resample("W-FRI").last().dropna()
    if len(w) >= 2:
        out["chg_week"] = pct(w.iloc[-1], w.iloc[-2]) if not is_rate else r((w.iloc[-1] - w.iloc[-2]) * 100, 1)
    for n in (20, 60, 120):
        if len(w) >= n:
            out[f"wma{n}"] = r(w.rolling(n).mean().iloc[-1])
    m = c.resample("ME").last().dropna()
    if len(m) >= 2 and not is_rate:
        out["chg_month"] = pct(m.iloc[-1], m.iloc[-2])

    if out.get("chg_1d") is not None and abs(out["chg_1d"]) >= (3 if id_ != "btc" else 6):
        signals.append(f"일간 급변 {out['chg_1d']:+.2f}%")
    if is_rate and out.get("chg_1d_bp") is not None and abs(out["chg_1d_bp"]) >= 10:
        signals.append(f"금리 급변 {out['chg_1d_bp']:+.1f}bp")

    out["signals"] = signals
    return out


# ---------------------------------------------------------------- 추가 지표 (방송에서 보는 것)

DAUM_HDR = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
            "Referer": "https://finance.daum.net/", "Accept": "application/json, text/plain, */*"}
FUT_COLS = ["외국인", "기관", "개인"]
KOSPI200_MULT = 250_000  # 코스피200 선물 거래승수(원/포인트)
ETF_RE = re.compile(r"KODEX|TIGER|KBSTAR|RISE |ACE |SOL |HANARO|PLUS |KIWOOM|KoAct|TIMEFOLIO|WON |1Q |BNK |"
                    r"UNICORN|FOCUS|TRUSTON|마이티|히어로즈|ARIRANG|KOSEF|파워 |VITA|에셋플러스|마이다스|KCGI|"
                    r"흥국|ITF|TREX|KTOP|ETN|레버리지|인버스|\(H\)|\(합성")


def daum_json(url: str) -> dict:
    res = requests.get(url, headers=DAUM_HDR, timeout=20)
    res.raise_for_status()
    return res.json()


def fetch_futures_flow(pages: int = 6) -> pd.DataFrame:
    """코스피200 선물(최근월물) 투자자별 순매수(계약). 다음 금융."""
    masters = daum_json("https://finance.daum.net/api/futures/masters")["data"]
    code = masters[0]["symbolCode"]
    rows = []
    for page in range(1, pages + 1):
        d = daum_json(f"https://finance.daum.net/api/future/{code}/days?pagination=true&page={page}&perPage=100")
        if not d.get("data"):
            break
        rows += d["data"]
        time.sleep(0.3)
    df = pd.DataFrame([{"date": pd.to_datetime(x["date"][:10]), "외국인": x.get("foreignSettlement"),
                        "기관": x.get("institutionSettlement"), "개인": x.get("privateSettlement"),
                        "종가": x.get("tradePrice"), "거래량": x.get("accTradeVolume"),
                        "월물": masters[0]["name"]} for x in rows]).set_index("date").sort_index()
    # 최근월물이 되기 전(원월물 시절) 거래가 거의 없던 기간과 장 시작 전 placeholder 행을 제외한다
    df = df[df["거래량"].fillna(0) >= 10000]
    return df[~df.index.duplicated(keep="last")]


def summarize_futures(df: pd.DataFrame) -> dict:
    df = df.dropna(subset=["외국인"])
    amt = lambda n, p: r(n * p * KOSPI200_MULT / 1e8, 0)  # noqa: E731  계약 → 억원(추정)
    last = df.iloc[-1]
    last_d = df.index[-1]
    week_start = last_d - pd.Timedelta(days=last_d.weekday())
    out = {"id": "kospi200_fut_flow", "name": "코스피200 선물 수급", "sector": "수급",
           "date": last_d.strftime("%Y-%m-%d"), "unit": "계약", "contract": last.get("월물"),
           "today": {}, "today_amt": {}, "sum_week": {}, "sum_5d": {}, "sum_20d": {}, "signals": []}
    for c in FUT_COLS:
        s = df[c]
        out["today"][c] = r(s.iloc[-1], 0)
        out["today_amt"][c] = amt(s.iloc[-1], last["종가"])
        out["sum_week"][c] = r(s[s.index >= week_start].sum(), 0)
        out["sum_5d"][c] = r(s.iloc[-5:].sum(), 0)
        out["sum_20d"][c] = r(s.iloc[-20:].sum(), 0)
    f = df["외국인"]
    sign, streak = np.sign(f.iloc[-1]), 0
    for v in f.iloc[::-1]:
        if np.sign(v) == sign and sign != 0:
            streak += 1
        else:
            break
    if streak >= 3:
        out["signals"].append(f"외국인 선물 {streak}일 연속 {'순매수' if sign > 0 else '순매도'}")
    if abs(out["today_amt"]["외국인"] or 0) >= 5000:
        out["signals"].append(f"외국인 선물 대량 {'순매수' if sign > 0 else '순매도'}(약 {out['today_amt']['외국인']:+,.0f}억)")
    return out


def fetch_foreign_ratio() -> pd.DataFrame:
    """코스피 외국인 보유 비중(%) = 종목별 외국인 지분율의 시가총액 가중 평균 (ETF·ETN 제외). 다음 금융."""
    rows, page = [], 1
    while page <= 20:
        d = daum_json("https://finance.daum.net/api/trend/market_capitalization?"
                      f"page={page}&perPage=200&fieldName=marketCap&order=desc&market=KOSPI&pagination=true")
        rows += d.get("data", [])
        if page >= d.get("totalPages", 1):
            break
        page += 1
        time.sleep(0.3)
    uniq = {x["code"]: x for x in rows}.values()
    stocks = [x for x in uniq if x.get("foreignRatio") not in (None, "") and not ETF_RE.search(x["name"])]
    cap = sum(x["marketCap"] for x in stocks)
    ratio = sum(x["marketCap"] * float(x["foreignRatio"]) for x in stocks) / cap * 100
    # 기준일 필드는 응답에 따라 없을 수 있다(10/26부터 평일 수집에서 KeyError 'baseDate').
    # 있는 값 중 최신을 쓰고, 없으면 KST 오늘(주말이면 직전 금요일)로 둔다.
    dates = [str(x.get(k))[:10] for x in stocks for k in ("baseDate", "tradeDate", "date") if x.get(k)]
    if not dates:
        print(f"WARN foreign_ratio: 기준일 필드 없음, KST 오늘로 저장. keys={sorted(stocks[0].keys())}", file=sys.stderr)
    if dates:
        base = max(dates)
    else:
        d = datetime.now(KST).date()
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        base = d.strftime("%Y-%m-%d")
    return pd.DataFrame({"close": [round(ratio, 3)], "stocks": [len(stocks)],
                         "mcap_trn": [round(cap / 1e12, 1)]}, index=[pd.to_datetime(base)])


def summarize_ratio(df: pd.DataFrame) -> dict:
    c = df["close"].dropna()
    back = lambda n: c.iloc[-1 - n] if len(c) > n else None  # noqa: E731
    out = {"id": "foreign_ratio", "name": "외국인 코스피 보유 비중", "sector": "수급", "unit": "%",
           "date": c.index[-1].strftime("%Y-%m-%d"), "close": r(c.iloc[-1], 2), "days": len(c), "signals": []}
    for k, n in (("chg_1d_pp", 1), ("chg_5d_pp", 5), ("chg_20d_pp", 20)):
        b = back(n)
        out[k] = None if b is None else r(c.iloc[-1] - b, 2)
    return out


def fetch_jgb30() -> pd.DataFrame:
    """일본 국채 30년 금리(%). 일본 재무성 일별 금리 CSV (전월까지 + 당월)."""
    base = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"
    urls = [base + "jgbcme.csv"]
    if not (PRICES / "jgb30y.csv").exists():
        urls.insert(0, base + "historical/jgbcme_all.csv")
    frames = []
    for u in urls:
        res = requests.get(u, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        res.raise_for_status()
        text = res.content.decode("utf-8", errors="ignore")
        lines = text.splitlines()
        h = next(i for i, ln in enumerate(lines) if ln.startswith("Date"))
        t = pd.read_csv(io.StringIO("\n".join(lines[h:])))
        t = t[["Date", "30Y"]].rename(columns={"Date": "date", "30Y": "close"})
        t["date"] = pd.to_datetime(t["date"], errors="coerce")
        t["close"] = pd.to_numeric(t["close"], errors="coerce")
        frames.append(t.dropna())
    df = pd.concat(frames).set_index("date").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df[df.index >= df.index.max() - pd.Timedelta(days=365 * 3)]


def summarize_flow(id_: str, name: str, df: pd.DataFrame) -> dict:
    df = df.dropna(how="all")
    out = {"id": id_, "name": name, "sector": "수급", "date": df.index[-1].strftime("%Y-%m-%d"),
           "unit": "억원", "today": {}, "sum_5d": {}, "sum_20d": {}, "sum_week": {}, "sum_ytd": {},
           "signals": []}
    last_d = df.index[-1]
    week_start = last_d - pd.Timedelta(days=last_d.weekday())
    for c in FLOW_COLS:
        s = df[c].dropna()
        if not s.empty:
            out["sum_week"][c] = r(s[s.index >= week_start].sum(), 0)
            out["sum_ytd"][c] = r(s[s.index.year == last_d.year].sum(), 0)
    for c in FLOW_COLS:
        s = df[c].dropna()
        if s.empty:
            continue
        out["today"][c] = r(s.iloc[-1], 0)
        out["sum_5d"][c] = r(s.iloc[-5:].sum(), 0)
        out["sum_20d"][c] = r(s.iloc[-20:].sum(), 0)
        # 연속 순매수/순매도 일수
        sign = np.sign(s.iloc[-1])
        streak = 0
        for v in s.iloc[::-1]:
            if np.sign(v) == sign and sign != 0:
                streak += 1
            else:
                break
        if c in ("외국인", "기관합계") and streak >= 5:
            out["signals"].append(f"{c} {streak}일 연속 {'순매수' if sign > 0 else '순매도'}")
    return out


# ---------------------------------------------------------------- 저장

def merge_save(path: Path, new: pd.DataFrame) -> pd.DataFrame:
    """기존 CSV에 새 데이터를 합친다. 같은 날짜는 새 값으로 덮어쓴다."""
    new = new.copy()
    new.index = pd.to_datetime(new.index)
    new.index.name = "date"
    if path.exists():
        old = pd.read_csv(path, index_col="date", parse_dates=["date"])
        new = pd.concat([old, new])
        new = new[~new.index.duplicated(keep="last")]
    new = new.sort_index()
    path.parent.mkdir(parents=True, exist_ok=True)
    new.to_csv(path, float_format="%.6g")
    return new


def main():
    now = datetime.now(KST)
    PRICES.mkdir(parents=True, exist_ok=True)
    FLOWS.mkdir(parents=True, exist_ok=True)
    items, errors = [], []

    for id_, name, sector, tk in YAHOO:
        try:
            df = merge_save(PRICES / f"{id_}.csv", fetch_yahoo(tk))
            items.append(summarize_price(id_, name, sector, df))
            print(f"ok  {id_}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            print(f"ERR {id_}: {e}", file=sys.stderr)

    for id_, name, sector, sid in FRED:
        try:
            df = merge_save(PRICES / f"{id_}.csv", fetch_fred(sid))
            items.append(summarize_price(id_, name, sector, df, is_rate=True))
            print(f"ok  {id_}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            print(f"ERR {id_}: {e}", file=sys.stderr)

    key = os.environ.get("ECOS_API_KEY", "").strip()
    for id_, name, sector, item in ECOS:
        if not key:
            errors.append(f"{name}: ECOS_API_KEY 미설정")
            continue
        try:
            df = merge_save(PRICES / f"{id_}.csv", fetch_ecos(item, key))
            items.append(summarize_price(id_, name, sector, df, is_rate=True))
            print(f"ok  {id_}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            print(f"ERR {id_}: {e}", file=sys.stderr)

    for id_, name, market in FLOW_MARKETS:
        try:
            df = merge_save(FLOWS / f"{id_}.csv", fetch_flow(market))
            items.append(summarize_flow(id_, name, df))
            print(f"ok  {id_}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            print(f"ERR {id_}: {e}", file=sys.stderr)

    extra = [
        ("코스피200 선물 수급", lambda: summarize_futures(merge_save(FLOWS / "kospi200_fut_flow.csv", fetch_futures_flow()))),
        ("외국인 코스피 보유 비중", lambda: summarize_ratio(merge_save(PRICES / "foreign_ratio.csv", fetch_foreign_ratio()))),
        ("일본 30년 국채", lambda: summarize_price("jgb30y", "일본 30년 국채", "금리",
                                                merge_save(PRICES / "jgb30y.csv", fetch_jgb30()), is_rate=True)),
    ]
    for name, fn in extra:
        try:
            items.append(fn())
            print(f"ok  {name}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:120]}")
            print(f"ERR {name}: {e}", file=sys.stderr)

    # 공개 저장소이므로 오류 문구에 비밀값이 남지 않게 한다
    if key:
        errors = [e.replace(key, "***") for e in errors]

    # 파생 지표: 미국채 2-10년 금리차
    by_id = {x["id"]: x for x in items}
    spreads = {}
    if "ust10y" in by_id and "ust2y" in by_id:
        spreads["us_10y_2y_bp"] = r((by_id["ust10y"]["close"] - by_id["ust2y"]["close"]) * 100, 1)
    if "ktb10y" in by_id and "ktb3y" in by_id:
        spreads["kr_10y_3y_bp"] = r((by_id["ktb10y"]["close"] - by_id["ktb3y"]["close"]) * 100, 1)

    # 날짜 검증: 오늘 한국 장이 열렸는데 국내 지표가 오늘 날짜가 아니면 표시
    today = now.strftime("%Y-%m-%d")
    stale = [x["name"] for x in items
             if x["id"] in ("kospi", "kosdaq", "samsung_elec", "sk_hynix", "kospi_flow", "kosdaq_flow")
             and x["date"] != today]

    latest = {"generated_at": now.isoformat(timespec="seconds"), "items": items,
              "spreads": spreads, "stale_domestic": stale, "errors": errors}
    (DATA / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")
    # 신호 이력: 날짜별 스냅샷 (같은 날 재실행 시 덮어씀)
    hist = DATA / "history"
    hist.mkdir(exist_ok=True)
    kd = by_id.get("kospi", {}).get("date", now.strftime("%Y-%m-%d"))  # 국내 거래일 기준 파일명
    (hist / f"{kd}.json").write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"완료: {len(items)}개 지표, 오류 {len(errors)}건, 국내 지연 {stale}")
    # 절반 이상 실패하면 워크플로를 실패로 표시해 알림을 받는다
    if len(items) < (len(YAHOO) + len(FRED) + len(ECOS) + len(FLOW_MARKETS)) / 2:
        sys.exit(1)


if __name__ == "__main__":
    main()
