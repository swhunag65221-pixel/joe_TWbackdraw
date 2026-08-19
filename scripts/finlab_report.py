#!/usr/bin/env python3
"""用 FinLab 的 `backtest.sim()` 回測本策略，產生可以 `report.display()` 的報表。

在 Jupyter / IPython 裡：

    import sys; sys.path.insert(0, "scripts")
    from finlab_report import build_report

    report = build_report()          # 預設參數（報酬÷回檔最佳）
    report.display()

    report = build_report(preset="post")        # 貼文原意
    report = build_report(preset="balanced")    # MA40 棘輪出場

直接執行（不開互動介面，只印摘要與驗證）：

    python scripts/finlab_report.py
    python scripts/finlab_report.py --preset post --start 2014-10-31

做法
----
訊號來自**加權指數**，部位開在 **00631L**。本檔把 `tw_backdraw` 的狀態機跑完，
再把每日的目標權重攤成 FinLab 要的 position DataFrame 交給 `sim()`。

時點對齊（最容易錯的一點）
--------------------------
`FinLab 的 position 日期是「訊號日」，實際成交落在次一交易日`
（trades 表裡 entry_sig_date 與 entry_date 差一天）。而 `tw_backdraw` 的
`Fill.d` 記的是**成交日**，所以權重必須往前挪一根 K 才對得上。
`verify_against_engine()` 會逐筆核對兩邊的進出場日期。

兩邊數字的對照方式
------------------
* FinLab trades 表的 `return` 是**個股報酬**；`tw_backdraw` 的 `Trade.ret` 是
  **權益報酬**。單一標的下 `權益報酬 ≈ 個股報酬 × 目標水位`（預設 0.8）。
* 總報酬會有 1~2% 的差距，主因是 FinLab 內建的價格資料通常比 repo 內的
  CSV 多一兩個交易日，未平倉部位的市值評價日不同；成本模型的細節也略有出入。

成本：ETF 的證交稅是 0.1%（非股票的 0.3%），手續費取 0.1425% × 0.6 折。
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tw_backdraw import load_csv                                    # noqa: E402
from tw_backdraw.backtest import align_etf                          # noqa: E402
from tw_backdraw.bars import Bar                                    # noqa: E402
from tw_backdraw.config import PRESETS, StrategyConfig              # noqa: E402
from tw_backdraw.engine import Engine, Result                       # noqa: E402

SYMBOL = "00631L"
TOKEN_ENV_VARS = ("Finlab_API_token", "FINLAB_API_TOKEN", "FINLAB_TOKEN")


# --------------------------------------------------------------------------
# 資料
# --------------------------------------------------------------------------
def login_finlab() -> None:
    token = next((os.environ[k] for k in TOKEN_ENV_VARS if os.environ.get(k)), None)
    if not token:
        raise SystemExit("找不到 FinLab token，請設定 Finlab_API_token 環境變數。")
    import finlab

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        finlab.login(token)


def load_bars(index_csv: str | Path | None = None,
              etf_csv: str | Path | None = None) -> tuple[list[Bar], list[float]]:
    """回傳對齊後的（加權指數日線, 00631L 收盤價）。

    優先讀 repo 內的 CSV（`scripts/fetch_finlab.py` 產生的還原股價）；
    找不到就直接向 FinLab 取。
    """
    index_csv = Path(index_csv or ROOT / "data" / "taiex.csv")
    etf_csv = Path(etf_csv or ROOT / "data" / f"{SYMBOL}.csv")

    if index_csv.exists() and etf_csv.exists():
        return align_etf(load_csv(index_csv), load_csv(etf_csv))

    login_finlab()
    from finlab import data

    idx = data.get("taiex_total_index:收盤指數").iloc[:, 0].dropna()
    etf = data.get("etl:adj_close")[SYMBOL].dropna()
    common = idx.index.intersection(etf.index)
    bars = [Bar(d=d.date(), open=float(idx[d]), high=float(idx[d]),
                low=float(idx[d]), close=float(idx[d])) for d in common]
    return bars, [float(etf[d]) for d in common]


# --------------------------------------------------------------------------
# 策略 → FinLab position
# --------------------------------------------------------------------------
def daily_weights(result: Result, bars: list[Bar]) -> dict:
    """把引擎的成交紀錄攤成 FinLab 要的「每日目標權重」。

    買進 → 權重累加；賣出 → 依賣出比例等比縮減。

    時點很容易錯：**FinLab 的 position 日期是訊號日，它在次一交易日成交**
    （trades 表裡的 entry_sig_date 與 entry_date 差一天）。而 `tw_backdraw`
    的 `Fill.d` 記的是**成交日**。所以權重必須往前挪一個交易日寫，
    否則會比策略本身晚一天進出場。
    """
    index_of = {b.d: i for i, b in enumerate(bars)}
    changes: dict[int, list] = {}
    for t in result.trades:
        for f in t.fills:
            # 成交日的前一根 K 就是引擎下決定的那天
            sig = max(index_of[f.d] - 1, 0)
            changes.setdefault(sig, []).append(f)

    weights, w = {}, 0.0
    for i, bar in enumerate(bars):
        for f in changes.get(i, []):
            w = w + f.weight if f.side == "buy" else w * (1.0 - f.weight)
        weights[bar.d] = 0.0 if w < 1e-9 else w
    return weights


def build_position(cfg: StrategyConfig | None = None,
                   index_csv=None, etf_csv=None, start: str | None = None,
                   end: str | None = None):
    """產生 FinLab `sim()` 要的 position DataFrame（單一標的 00631L）。"""
    import pandas as pd

    cfg = cfg or PRESETS["tuned"]
    bars, etf = load_bars(index_csv, etf_csv)
    result = Engine(cfg).run(bars, etf)
    weights = daily_weights(result, bars)

    pos = pd.DataFrame(
        {SYMBOL: [weights[b.d] for b in bars]},
        index=pd.to_datetime([b.d for b in bars]),
    )
    if start:
        pos = pos[pos.index >= start]
    if end:
        pos = pos[pos.index <= end]
    return pos, result, bars, etf


def build_report(preset: str = "tuned", cfg: StrategyConfig | None = None,
                 index_csv=None, etf_csv=None, start: str | None = None,
                 end: str | None = None, name: str | None = None, **sim_kwargs):
    """跑 `finlab.backtest.sim()`，回傳可以 `.display()` 的 Report。"""
    cfg = cfg or PRESETS[preset]
    pos, *_ = build_position(cfg, index_csv, etf_csv, start, end)

    login_finlab()
    from finlab.backtest import sim

    params = dict(
        trade_at_price="close",
        position_limit=1,
        fee_ratio=cfg.cost.buy_cost,          # 0.1425% × 0.6 折
        tax_ratio=cfg.cost.tax_rate,          # ETF 0.1%，非股票的 0.3%
        name=name or f"台灣指數快速修復 {preset}（{SYMBOL}）",
        upload=False,
    )
    params.update(sim_kwargs)
    return sim(pos, **params)


# --------------------------------------------------------------------------
# 驗證：FinLab 的結果應該與 tw_backdraw 內建回測一致
# --------------------------------------------------------------------------
def verify_against_engine(preset: str = "tuned", **kwargs) -> dict:
    """比對 FinLab 與 `tw_backdraw` 內建回測，逐筆核對進出場日期。"""
    from tw_backdraw.backtest import summarize

    cfg = PRESETS[preset]
    pos, result, bars, etf = build_position(cfg, **kwargs)
    own = summarize(result)

    report = build_report(preset=preset, **kwargs)
    eq = report.creturn
    trades = report.trades.reset_index()

    rows, mismatches = [], 0
    for i, t in enumerate(result.trades):
        if i >= len(trades):
            break
        f = trades.iloc[i]
        f_in = f["entry_date"].date()
        f_out = f["exit_date"].date() if f["exit_date"] == f["exit_date"] else None
        own_in = t.fills[0].d if t.fills else None
        own_out = t.fills[-1].d if len(t.fills) > 1 else None
        same = (f_in == own_in) and (f_out == own_out)
        mismatches += not same
        rows.append(dict(own_entry=own_in, finlab_entry=f_in,
                         own_exit=own_out, finlab_exit=f_out,
                         own_ret=t.ret, finlab_ret=float(f["return"]), match=same))

    return {
        "own_total": own.total_return,
        "finlab_total": float(eq.iloc[-1] / eq.iloc[0] - 1),
        "own_mdd": own.max_drawdown,
        "finlab_mdd": float((eq / eq.cummax() - 1).min()),
        "own_trades": own.n_trades, "finlab_trades": len(trades),
        "position_days": int((pos[SYMBOL] > 0).sum()),
        "rows": rows, "mismatches": mismatches, "report": report,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="用 FinLab sim 回測本策略")
    ap.add_argument("--preset", default="tuned", choices=sorted(PRESETS))
    ap.add_argument("--start", help="回測起日 YYYY-MM-DD")
    ap.add_argument("--end", help="回測迄日 YYYY-MM-DD")
    ap.add_argument("--display", action="store_true", help="呼叫 report.display()")
    args = ap.parse_args()

    v = verify_against_engine(preset=args.preset, start=args.start, end=args.end)
    print(f"\n預設組 {args.preset}")
    print(f"  在市天數        {v['position_days']}")
    print(f"  交易筆數  自建 {v['own_trades']:>3}   FinLab {v['finlab_trades']:>3}")
    print(f"  總報酬    自建 {v['own_total']:>9.1%}   FinLab {v['finlab_total']:>9.1%}")
    print(f"  最大回檔  自建 {v['own_mdd']:>9.1%}   FinLab {v['finlab_mdd']:>9.1%}")

    print(f"\n逐筆核對（FinLab 的 return 是個股報酬，自建是權益報酬 ≈ 個股 × 目標水位）")
    print(f"  {'進場':<12}{'出場':<12}{'自建(權益)':>11}{'FinLab(個股)':>13}   對齊")
    for r in v["rows"]:
        print(f"  {r['own_entry']!s:<12}{r['own_exit'] or '持有中'!s:<12}"
              f"{r['own_ret']:>11.1%}{r['finlab_ret']:>13.1%}   "
              f"{'✓' if r['match'] else '✗ 日期不一致'}")
    if v["mismatches"]:
        print(f"\n  ⚠ 有 {v['mismatches']} 筆進出場日期對不上，請檢查時點對齊邏輯")
    else:
        print(f"\n  ✓ {len(v['rows'])} 筆進出場日期全部一致")

    if args.display:
        v["report"].display()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
