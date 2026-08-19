"""命令列介面。

    python -m tw_backdraw plan --peak 47742 --trough 39933 --now 46200 --capital 1000000
    python -m tw_backdraw scan --csv data/taiex.csv
    python -m tw_backdraw episodes --csv data/taiex.csv --since 2005-01-01
    python -m tw_backdraw status --csv data/taiex.csv --capital 1000000
    python -m tw_backdraw backtest --csv data/taiex.csv
"""

from __future__ import annotations

import argparse

from .backtest import align_etf, run_backtest
from .bars import load_csv
from .config import (
    DEFAULT_CONFIG, PRESETS, EntryConfig, SetupConfig, SizingConfig, StrategyConfig,
)
from .plan import build_plan
from .setup import detect_setups, scan_episodes
from .status import render_status


def _config_from_args(args: argparse.Namespace) -> StrategyConfig:
    cfg = PRESETS.get(getattr(args, "preset", None) or "post", DEFAULT_CONFIG)
    setup = SetupConfig(
        min_drawdown=getattr(args, "min_drawdown", None) or cfg.setup.min_drawdown,
        repair_fraction=getattr(args, "repair", None) or cfg.setup.repair_fraction,
        max_repair_bars=getattr(args, "max_bars", None) or cfg.setup.max_repair_bars,
        setup_expiry_bars=cfg.setup.setup_expiry_bars,
        reanchor_on_expiry=cfg.setup.reanchor_on_expiry,
    )
    sizing = SizingConfig(
        risk_per_trade=getattr(args, "risk", None) or cfg.sizing.risk_per_trade,
        leverage=cfg.sizing.leverage,
        max_weight=cfg.sizing.max_weight,
        min_stop_distance=cfg.sizing.min_stop_distance,
    )
    entry = EntryConfig(
        base_weight=getattr(args, "base_weight", None) or cfg.entry.base_weight,
        pullback_ladder=cfg.entry.pullback_ladder,
        fill_timeout_bars=cfg.entry.fill_timeout_bars,
        breakout_fills_remainder=cfg.entry.breakout_fills_remainder,
    )
    return StrategyConfig(setup=setup, levels=cfg.levels, entry=entry,
                          exit=cfg.exit, sizing=sizing, cost=cfg.cost)


def cmd_plan(args: argparse.Namespace) -> int:
    cfg = _config_from_args(args)
    now = args.now if args.now is not None else args.peak
    print(build_plan(args.peak, args.trough, now, cfg, args.capital).render())
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    cfg = _config_from_args(args)
    bars = load_csv(args.csv)
    setups = detect_setups(bars, cfg.setup)
    print(f"{bars[0].d} ~ {bars[-1].d}，{len(bars)} 根日線，觸發 {len(setups)} 次快速修復訊號\n")
    for s in setups:
        print("  " + s.describe())
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    cfg = _config_from_args(args)
    bars = load_csv(args.csv)
    etf = None
    if args.etf_csv:
        bars, etf = align_etf(bars, load_csv(args.etf_csv))
        print(f"使用 {args.etf_csv} 的真實 ETF 價格，回測期間縮至重疊區間")
    result, stats = run_backtest(bars, cfg, etf)
    print(f"回測期間 {bars[0].d} ~ {bars[-1].d}\n")
    print(stats.render())
    if args.verbose:
        for t in result.trades:
            print("\n" + "-" * 60)
            print(t.setup.describe())
            print(f"目標水位 {t.target_weight:.0%}｜結果 {t.ret:+.1%}｜出場原因 {t.exit_reason}")
            for f in t.fills:
                print("   " + f.describe())
    return 0


def cmd_episodes(args: argparse.Namespace) -> int:
    """列出每一段回檔，以及它為什麼（沒）觸發訊號。"""
    cfg = _config_from_args(args)
    bars = load_csv(args.csv)
    eps = scan_episodes(bars, cfg.setup)
    if args.since:
        eps = [e for e in eps if e.end_date.isoformat() >= args.since]
    if args.until:
        eps = [e for e in eps if e.peak_date.isoformat() <= args.until]
    fired = sum(1 for e in eps if e.fired)
    span = f"{args.since or bars[0].d} ~ {args.until or bars[-1].d}"
    if args.since or args.until:
        span += "（已篩選）"
    print(f"{span}：{len(eps)} 段 ≥{cfg.setup.min_drawdown:.0%} 的回檔，"
          f"其中 {fired} 段觸發訊號\n")
    print(f"{'高點':<22}{'谷底':<22}{'跌幅':>7}{'破底':>5}  判定")
    print("-" * 96)
    for e in eps:
        print(f"{e.peak_date!s} {e.peak:>9,.0f}  {e.trough_date!s} {e.trough:>9,.0f}"
              f"{e.drop_pct:>7.1%}{e.lower_lows:>5}  {e.reason(cfg.setup)}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _config_from_args(args)
    bars = load_csv(args.csv)
    etf = None
    if args.etf_csv:
        bars, etf = align_etf(bars, load_csv(args.etf_csv))
    result, _ = run_backtest(bars, cfg, etf)
    print(render_status(result, bars, cfg, args.capital))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tw_backdraw", description="台灣指數快速修復回檔入場策略")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--min-drawdown", type=float, help="辨識回檔的最小跌幅，預設 0.10")
    common.add_argument("--repair", type=float, help="補回比例門檻，預設 0.75")
    common.add_argument("--max-bars", type=int, help="補回的最大交易日數，預設 15")
    common.add_argument("--risk", type=float, help="單筆風險預算，預設 0.08")
    common.add_argument("--base-weight", type=float, help="底倉佔目標部位比例，預設 0.40")
    common.add_argument("--preset", choices=sorted(PRESETS),
                        help="參數預設組：post（貼文原意，預設）/ tuned（報酬最佳）"
                             "/ balanced（調校訊號 + MA40 出場）/ winrate（勝率最高，無停損）")

    sp = sub.add_parser("plan", parents=[common], help="產生操作計畫")
    sp.add_argument("--peak", type=float, required=True, help="前波高點")
    sp.add_argument("--trough", type=float, required=True, help="波段谷底")
    sp.add_argument("--now", type=float, help="目前指數（預設用前高）")
    sp.add_argument("--capital", type=float, help="投入本金，用來換算金額")
    sp.set_defaults(func=cmd_plan)

    ss = sub.add_parser("scan", parents=[common], help="掃描歷史訊號")
    ss.add_argument("--csv", required=True, help="指數日線 CSV（date,close 為必要欄位）")
    ss.set_defaults(func=cmd_scan)

    se = sub.add_parser("episodes", parents=[common],
                        help="列出每一段回檔與它（沒）觸發訊號的原因")
    se.add_argument("--csv", required=True, help="指數日線 CSV")
    se.add_argument("--since", help="只看這個日期之後結束的段落 YYYY-MM-DD")
    se.add_argument("--until", help="只看這個日期之前起漲的段落 YYYY-MM-DD")
    se.set_defaults(func=cmd_episodes)

    st = sub.add_parser("status", parents=[common], help="目前部位現況與下一步")
    st.add_argument("--csv", required=True, help="指數日線 CSV")
    st.add_argument("--etf-csv", help="00631L 實際日線 CSV")
    st.add_argument("--capital", type=float, help="投入本金，用來換算金額")
    st.set_defaults(func=cmd_status)

    sb = sub.add_parser("backtest", parents=[common], help="回測")
    sb.add_argument("--csv", required=True, help="指數日線 CSV")
    sb.add_argument("--etf-csv", help="00631L 實際日線 CSV；未提供則由指數合成 2 倍路徑")
    sb.add_argument("-v", "--verbose", action="store_true", help="列出每筆交易明細")
    sb.set_defaults(func=cmd_backtest)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
