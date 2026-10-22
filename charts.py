"""리포트용 정적 차트(PNG) 생성.

docs/charts/<거래일>/<id>.png 로 저장되고 GitHub Pages 로 배포된다.
리포트(프로젝트 문서)는 이 이미지 주소를 불러와 표시한다.
- 가격 지표: 최근 120영업일 캔들(또는 선) + 20/60/120일선 + 볼린저밴드
- 수급: 외국인·개인·기관 일별 순매수 막대 + 외국인 누적선 (최근 60영업일)
120일보다 오래된 날짜 폴더는 지운다.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib import font_manager  # noqa: E402

from collect import ECOS, FLOW_MARKETS, FRED, YAHOO  # noqa: E402

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = ROOT / "docs" / "charts"
KEEP_DAYS = 120

UP, DN = "#d93a3a", "#2f6fdb"
MA = {20: "#e0a100", 60: "#16a34a", 120: "#8b5cf6"}


def nf(v):
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.2f}"


def set_font():
    for name in ("NanumGothic", "Noto Sans CJK KR", "Noto Sans KR", "AppleGothic"):
        if any(name in f.name for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = name
            break
    plt.rcParams["axes.unicode_minus"] = False


def date_axis(ax, idx, n=6):
    """거래일 순번을 x로 쓰고 날짜 라벨만 붙인다 (주말·휴일 공백 제거)."""
    step = max(1, len(idx) // n)
    ticks = list(range(0, len(idx), step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([idx[i].strftime("%m/%d") for i in ticks])
    ax.set_xlim(-1, len(idx) + 3)


def style(ax):
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color("#cbd5e1")
    ax.tick_params(colors="#475569", labelsize=8, length=0)
    ax.yaxis.tick_right()
    ax.grid(axis="y", color="#eef1f5", lw=0.8)


def price_chart(id_, name, is_rate, latest, out_dir):
    p = DATA / "prices" / f"{id_}.csv"
    if not p.exists():
        return None
    full = pd.read_csv(p, index_col="date", parse_dates=["date"]).sort_index()
    c = full["close"]
    ma = {n: c.rolling(n).mean() for n in MA}
    mid, sd = c.rolling(20).mean(), c.rolling(20).std()
    df = full.iloc[-120:]
    idx = df.index
    x = list(range(len(idx)))

    fig, ax = plt.subplots(figsize=(8, 3.4), dpi=100)
    flat = is_rate or df[["open", "high", "low"]].isna().all().all() or (df["open"] == df["close"]).all()
    if flat:
        ax.plot(x, df["close"], color="#1d2330", lw=1.6)
    else:
        o, h, l, cl = (df[k].fillna(df["close"]) for k in ("open", "high", "low", "close"))
        col = [UP if b >= a else DN for a, b in zip(o, cl)]
        ax.vlines(x, l, h, colors=col, lw=0.8)
        ax.bar(x, (cl - o).abs().clip(lower=(cl.abs() * 0.0008)), bottom=pd.concat([o, cl], axis=1).min(axis=1),
               color=col, width=0.65)
    for n, color in MA.items():
        ax.plot(x, ma[n].reindex(idx), color=color, lw=1.1, label=f"{n}일")
    ax.plot(x, (mid + 2 * sd).reindex(idx), color="#94a3b8", lw=0.8, ls="--", label="볼린저")
    ax.plot(x, (mid - 2 * sd).reindex(idx), color="#94a3b8", lw=0.8, ls="--")

    L = latest or {}
    last = df["close"].iloc[-1]
    if is_rate:
        sub = f"{last:.2f}%  ({L.get('chg_1d_bp', 0):+}bp)" if L.get("chg_1d_bp") is not None else f"{last:.2f}%"
    else:
        sub = f"{nf(last)}  ({L.get('chg_1d'):+.2f}%)" if L.get("chg_1d") is not None else nf(last)
    ax.set_title(f"{name}  {sub}", loc="left", fontsize=11, fontweight="bold", color="#1d2330")
    ax.annotate(nf(last), (x[-1], last), xytext=(4, 0), textcoords="offset points",
                fontsize=8, color="#1d2330", va="center")
    style(ax)
    date_axis(ax, idx)
    ax.legend(loc="lower right", bbox_to_anchor=(1, 1.0), fontsize=7, frameon=False, ncol=4, borderaxespad=0.2)
    fig.tight_layout()
    path = out_dir / f"{id_}.png"
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    return path


def flow_chart(id_, name, out_dir):
    p = DATA / "flows" / f"{id_}.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p, index_col="date", parse_dates=["date"]).sort_index().fillna(0).iloc[-60:]
    x = list(range(len(df)))
    f = df["외국인"]
    fig, ax = plt.subplots(figsize=(8, 3.4), dpi=100)
    ax.bar(x, f, color=[UP if v >= 0 else DN for v in f], width=0.7, label="외국인 일별")
    ax.axhline(0, color="#cbd5e1", lw=0.8)
    ax2 = ax.twinx()
    ax2.plot(x, f.cumsum(), color="#1d2330", lw=1.6, label="외국인 60일 누적")
    ax2.plot(x, df["개인"].cumsum(), color="#94a3b8", lw=1.2, ls="--", label="개인 60일 누적")
    ax2.tick_params(labelsize=8, colors="#475569", length=0)
    for sp in ax2.spines.values():
        sp.set_visible(False)
    ax.set_title(f"{name}  외국인 (60일, 억원)", loc="left", fontsize=11, fontweight="bold",
                 color="#1d2330")
    style(ax)
    ax.yaxis.tick_left()
    date_axis(ax, df.index)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="lower right", bbox_to_anchor=(1, 1.0), fontsize=7, frameon=False, ncol=3,
              borderaxespad=0.2)
    fig.tight_layout()
    path = out_dir / f"{id_}.png"
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    return path


def main():
    set_font()
    latest = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
    by_id = {x["id"]: x for x in latest["items"]}
    day = by_id.get("kospi", {}).get("date") or latest["generated_at"][:10]
    out_dir = OUT / day
    out_dir.mkdir(parents=True, exist_ok=True)

    made = []
    for id_, name, _, _ in YAHOO:
        if price_chart(id_, name, False, by_id.get(id_), out_dir):
            made.append(id_)
    for id_, name, _, _ in FRED + ECOS:
        if price_chart(id_, name, True, by_id.get(id_), out_dir):
            made.append(id_)
    for id_, name, _ in FLOW_MARKETS:
        if flow_chart(id_, name, out_dir):
            made.append(id_)
    (out_dir / "index.json").write_text(json.dumps(made), encoding="utf-8")

    # 오래된 날짜 폴더 정리
    cutoff = datetime.now() - timedelta(days=KEEP_DAYS)
    for d in OUT.iterdir():
        try:
            if d.is_dir() and datetime.strptime(d.name, "%Y-%m-%d") < cutoff:
                shutil.rmtree(d)
        except ValueError:
            pass
    print(f"charts {day}: {len(made)}개")


if __name__ == "__main__":
    main()
