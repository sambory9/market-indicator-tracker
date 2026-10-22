"""data/ 의 CSV와 latest.json 을 대시보드용 docs/data.json 으로 변환한다.

- 일봉: 최근 400영업일
- 주봉: 전체 기간을 주(금) 단위 OHLC로 묶음
- 이동평균·볼린저·RSI·MACD 는 프런트에서 계산한다 (원본 OHLC만 보낸다)
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from collect import ECOS, FLOW_MARKETS, FRED, OSC_IDS, YAHOO

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"

SECTOR_ORDER = ["수급", "지수", "종목", "환율", "금리", "원자재/코인", "선물"]


def ohlc(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in ("open", "high", "low"):
        if c not in df or df[c].isna().all():
            df[c] = df["close"]
        df[c] = df[c].fillna(df["close"])
    return df[["open", "high", "low", "close"]].dropna()


def weekly(df: pd.DataFrame) -> pd.DataFrame:
    return df.resample("W-FRI").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()


def rows(df: pd.DataFrame, nd: int) -> list:
    return [[i.strftime("%Y-%m-%d"), *(round(float(v), nd) for v in r)]
            for i, r in zip(df.index, df.itertuples(index=False))]


def main():
    latest = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
    by_id = {x["id"]: x for x in latest["items"]}
    series = []

    defs = [(i, n, s, False) for i, n, s, _ in YAHOO] + [(i, n, s, True) for i, n, s, _ in FRED + ECOS]
    for id_, name, sector, is_rate in defs:
        p = DATA / "prices" / f"{id_}.csv"
        if not p.exists():
            continue
        df = ohlc(pd.read_csv(p, index_col="date", parse_dates=["date"]).sort_index())
        nd = 3 if is_rate or id_ == "usdjpy" else 2
        series.append({
            "id": id_, "name": name, "sector": sector, "rate": is_rate, "osc": id_ in OSC_IDS,
            "daily": rows(df.iloc[-400:], nd), "weekly": rows(weekly(df), nd),
            "latest": by_id.get(id_),
        })

    flows = []
    for id_, name, _ in FLOW_MARKETS:
        p = DATA / "flows" / f"{id_}.csv"
        if not p.exists():
            continue
        df = pd.read_csv(p, index_col="date", parse_dates=["date"]).sort_index().fillna(0)
        flows.append({"id": id_, "name": name, "cols": list(df.columns),
                      "rows": [[i.strftime("%Y-%m-%d"), *map(float, r)] for i, r in
                               zip(df.index, df.itertuples(index=False))],
                      "latest": by_id.get(id_)})

    out = {"generated_at": latest["generated_at"], "spreads": latest.get("spreads", {}),
           "errors": latest.get("errors", []), "sectors": SECTOR_ORDER,
           "series": series, "flows": flows}
    DOCS.mkdir(exist_ok=True)
    (DOCS / "data.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"docs/data.json: {len(series)} series, {len(flows)} flows")


if __name__ == "__main__":
    main()
