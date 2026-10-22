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
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    body = r.json()
    rows = body.get("StatisticSearch", {}).get("row", [])
    if not rows:
        raise RuntimeError(f"ECOS 응답 없음: {body.get('RESULT', body)}")
    df = pd.DataFrame({"date": pd.to_datetime([x["TIME"] for x in rows], format="%Y%m%d"),
                       "close": pd.to_numeric([x["DATA_VALUE"] for x in rows], errors="coerce")})
    return df.dropna().set_index("date")


def fetch_flow(market: str) -> pd.DataFrame:
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

    if out.get("chg_1d") is not None and abs(out["chg_1d"]) >= (3 if id_ != "btc" else 6):
        signals.append(f"일간 급변 {out['chg_1d']:+.2f}%")
    if is_rate and out.get("chg_1d_bp") is not None and abs(out["chg_1d_bp"]) >= 10:
        signals.append(f"금리 급변 {out['chg_1d_bp']:+.1f}bp")

    out["signals"] = signals
    return out


def summarize_flow(id_: str, name: str, df: pd.DataFrame) -> dict:
    df = df.dropna(how="all")
    out = {"id": id_, "name": name, "sector": "수급", "date": df.index[-1].strftime("%Y-%m-%d"),
           "unit": "억원", "today": {}, "sum_5d": {}, "sum_20d": {}, "signals": []}
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
    print(f"완료: {len(items)}개 지표, 오류 {len(errors)}건, 국내 지연 {stale}")
    # 절반 이상 실패하면 워크플로를 실패로 표시해 알림을 받는다
    if len(items) < (len(YAHOO) + len(FRED) + len(ECOS) + len(FLOW_MARKETS)) / 2:
        sys.exit(1)


if __name__ == "__main__":
    main()
