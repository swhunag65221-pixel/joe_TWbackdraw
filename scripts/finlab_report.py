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

TOKEN_ENV_VARS = ("Finlab_API_token", "FINLAB_API_TOKEN", "FINLAB_TOKEN")

# 市場設定。leverage / fee / tax 會覆蓋 config 裡的對應欄位。
MARKETS = {
    "tw": dict(symbol="00631L", index_csv="taiex.csv", index_name="加權指數",
               leverage=2.0, fee=0.001425 * 0.60, tax=0.001, carry=0.012,
               finlab_market=None),
    "us": dict(symbol="UPRO", index_csv="gspc.csv", index_name="S&P 500",
               leverage=3.0, fee=0.0, tax=0.0, carry=0.0091,
               finlab_market="US_STOCK"),
}
SYMBOL = MARKETS["tw"]["symbol"]      # 相容舊呼叫


def us_fund_market():
    """FinLab 內建的 `market="US_STOCK"` 只讀 `us_price`（個股），裡面沒有 UPRO。

    槓桿 ETF 在 `us_fund_price`，所以這裡子類化 `USMarket`、把價格表指過去。
    benchmark 沿用它原本的 `^GSPC`，正好就是我們的訊號來源。
    """
    from typing import ClassVar

    from finlab.markets.us import USMarket

    class USFundMarket(USMarket):
        _price_table: ClassVar[str] = "us_fund_price"
        _adj_prefix: ClassVar[str] = "us_fund_price:adj_"

        @staticmethod
        def get_name() -> str:
            return "us_fund"

        @staticmethod
        def get_asset_id_to_name() -> dict:
            # us_company_profile 只涵蓋個股，ETF 查不到；回傳空 dict 讓它退回顯示代號
            return {}

    return USFundMarket()


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
              etf_csv: str | Path | None = None,
              market: str = "tw") -> tuple[list[Bar], list[float]]:
    """回傳對齊後的（指數日線, 槓桿 ETF 收盤價）。

    讀 repo 內的 CSV —— 由 `scripts/fetch_finlab.py` 產生（ETF 一律還原股價）。
    """
    m = MARKETS[market]
    index_csv = Path(index_csv or ROOT / "data" / m["index_csv"])
    etf_csv = Path(etf_csv or ROOT / "data" / f"{m['symbol']}.csv")
    for f in (index_csv, etf_csv):
        if not f.exists():
            raise SystemExit(
                f"找不到 {f}。請先執行："
                + ("python3 scripts/fetch_finlab.py --us" if market == "us"
                   else "python3 scripts/fetch_finlab.py"))
    return align_etf(load_csv(index_csv), load_csv(etf_csv))


def market_config(cfg: StrategyConfig, market: str) -> StrategyConfig:
    """把槓桿倍數與交易成本換成該市場的設定。"""
    from dataclasses import replace

    from tw_backdraw.config import CostConfig, SizingConfig

    m = MARKETS[market]
    return replace(
        cfg,
        sizing=SizingConfig(risk_per_trade=cfg.sizing.risk_per_trade,
                            leverage=m["leverage"], max_weight=cfg.sizing.max_weight,
                            min_stop_distance=cfg.sizing.min_stop_distance),
        cost=CostConfig(fee_rate=m["fee"], fee_discount=1.0, tax_rate=m["tax"],
                        annual_carry=m["carry"], trading_days=cfg.cost.trading_days),
    )


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
                   end: str | None = None, market: str = "tw"):
    """產生 FinLab `sim()` 要的 position 與成交價 DataFrame（單一槓桿 ETF）。

    成交價一併輸出，是因為 FinLab 內部的價格表保留了休市日的 NaN 列
    （UPRO 全期 235 天、^GSPC 105 天）。若讓它自己挑成交日，訊號隔天可能
    正好落在 NaN 列上，該筆交易的成交價與報酬都會變成 NaN。改用這裡清理過的
    價格（已 dropna 並與指數對齊），兩邊的計算基礎才完全一致。
    """
    import pandas as pd

    symbol = MARKETS[market]["symbol"]
    cfg = market_config(cfg or PRESETS["tuned"], market)
    bars, etf = load_bars(index_csv, etf_csv, market)
    result = Engine(cfg).run(bars, etf)
    weights = daily_weights(result, bars)

    idx = pd.to_datetime([b.d for b in bars])
    pos = pd.DataFrame({symbol: [weights[b.d] for b in bars]}, index=idx)
    price = pd.DataFrame({symbol: etf}, index=idx)
    if start:
        pos, price = pos[pos.index >= start], price[price.index >= start]
    if end:
        pos, price = pos[pos.index <= end], price[price.index <= end]
    return pos, result, bars, etf, price


def build_report(preset: str = "tuned", cfg: StrategyConfig | None = None,
                 index_csv=None, etf_csv=None, start: str | None = None,
                 end: str | None = None, name: str | None = None,
                 market: str = "tw", **sim_kwargs):
    """跑 `finlab.backtest.sim()`，回傳可以 `.display()` 的 Report。"""
    m = MARKETS[market]
    cfg = market_config(cfg or PRESETS[preset], market)
    pos, _res, _bars, _etf, price = build_position(cfg, index_csv, etf_csv,
                                                   start, end, market)

    login_finlab()
    from finlab.backtest import sim

    params = dict(
        # 直接餵清理過的收盤價，避免 FinLab price table 裡的休市日 NaN 列
        trade_at_price=price,
        position_limit=1,
        fee_ratio=cfg.cost.buy_cost,
        tax_ratio=cfg.cost.tax_rate,
        name=name or f"{m['index_name']}快速修復 {preset}（{m['symbol']}）",
        upload=False,
    )
    if m["finlab_market"] == "US_STOCK":
        params["market"] = us_fund_market()
    elif m["finlab_market"]:
        params["market"] = m["finlab_market"]
    params.update(sim_kwargs)
    return sim(pos, **params)


# --------------------------------------------------------------------------
# 驗證：FinLab 的結果應該與 tw_backdraw 內建回測一致
# --------------------------------------------------------------------------
def verify_against_engine(preset: str = "tuned", market: str = "tw", **kwargs) -> dict:
    """比對 FinLab 與 `tw_backdraw` 內建回測，逐筆核對進出場日期。"""
    from tw_backdraw.backtest import summarize

    cfg = PRESETS[preset]
    pos, result, bars, etf, _price = build_position(cfg, market=market, **kwargs)
    own = summarize(result)

    report = build_report(preset=preset, market=market, **kwargs)
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
        "position_days": int((pos[MARKETS[market]["symbol"]] > 0).sum()),
        "rows": rows, "mismatches": mismatches, "report": report,
    }


KEY_STATS = ("cagr", "total_return", "max_drawdown", "daily_sharpe",
             "daily_sortino", "calmar", "win_ratio")


def key_metrics(report) -> dict:
    """抽出 CAGR / 總報酬 / 最大回檔 / Sharpe / Sortino / Calmar / 勝率。"""
    st = report.get_stats()
    return {k: st.get(k) for k in KEY_STATS}


def buy_and_hold_metrics(symbol: str, market: str = "us",
                         start=None, end=None) -> dict:
    """同期買進持有的對照組，指標算法與 FinLab 的 get_stats 對齊。"""
    import numpy as np
    import pandas as pd

    login_finlab()
    from finlab import data

    src = ("us_fund_price:adj_close" if market == "us" and symbol != "^GSPC"
           else "world_index:adj_close" if symbol.startswith("^")
           else "etl:adj_close")
    px = data.get(src)[symbol].dropna()
    if start:
        px = px[px.index >= start]
    if end:
        px = px[px.index <= end]

    ret = px.pct_change().dropna()
    years = (px.index[-1] - px.index[0]).days / 365.25
    total = float(px.iloc[-1] / px.iloc[0] - 1)
    mdd = float((px / px.cummax() - 1).min())
    return {
        "cagr": (1 + total) ** (1 / years) - 1,
        "total_return": total,
        "max_drawdown": mdd,
        "daily_sharpe": float(ret.mean() / ret.std() * np.sqrt(252)),
        "daily_sortino": float(ret.mean() / ret[ret < 0].std() * np.sqrt(252)),
        "calmar": ((1 + total) ** (1 / years) - 1) / abs(mdd),
        "win_ratio": float((ret > 0).mean()),
    }


def render_metrics(rows: list[tuple[str, dict]]) -> str:
    head = (f"{'':<30}{'CAGR':>9}{'總報酬':>11}{'最大回檔':>10}"
            f"{'Sharpe':>9}{'Sortino':>9}{'Calmar':>9}")
    out = [head, "-" * 78]
    for name, m in rows:
        def f(k, pct=True):
            v = m.get(k)
            if v is None:
                return f"{'—':>9}"
            return f"{v:>9.1%}" if pct else f"{v:>9.2f}"
        out.append(f"{name:<30}{f('cagr')}{f('total_return'):>11}"
                   f"{f('max_drawdown'):>10}{f('daily_sharpe', False)}"
                   f"{f('daily_sortino', False)}{f('calmar', False)}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="用 FinLab sim 回測本策略")
    ap.add_argument("--preset", default="tuned", choices=sorted(PRESETS))
    ap.add_argument("--market", default="tw", choices=sorted(MARKETS),
                    help="tw = 加權指數/00631L（2x）；us = S&P500/UPRO（3x）")
    ap.add_argument("--start", help="回測起日 YYYY-MM-DD")
    ap.add_argument("--end", help="回測迄日 YYYY-MM-DD")
    ap.add_argument("--display", action="store_true", help="呼叫 report.display()")
    ap.add_argument("--stats", action="store_true",
                    help="只印 CAGR / MDD / Sharpe 等指標，並與買進持有對照")
    args = ap.parse_args()

    if args.stats:
        m = MARKETS[args.market]
        rep = build_report(preset=args.preset, market=args.market,
                           start=args.start, end=args.end)
        pos, *_ = build_position(PRESETS[args.preset], start=args.start,
                                 end=args.end, market=args.market)
        pos = pos
        lo, hi = pos.index[0], pos.index[-1]
        rows = [(f"策略 {args.preset}（{m['symbol']}）", key_metrics(rep))]
        bench = [m["symbol"]] + (["SPY", "^GSPC"] if args.market == "us" else ["0050"])
        for b in bench:
            try:
                rows.append((f"買進持有 {b}", buy_and_hold_metrics(b, args.market, lo, hi)))
            except Exception as exc:
                print(f"  （{b} 無法取得：{exc}）")
        print(f"\n{m['index_name']} → {m['symbol']}（{m['leverage']:g}x）　"
              f"{lo.date()} ~ {hi.date()}")
        print(render_metrics(rows))
        return 0

    v = verify_against_engine(preset=args.preset, market=args.market,
                              start=args.start, end=args.end)
    m = MARKETS[args.market]
    print(f"\n{m['index_name']} → {m['symbol']}（{m['leverage']:g}x）　預設組 {args.preset}")
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
