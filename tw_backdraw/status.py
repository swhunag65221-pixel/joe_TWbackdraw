"""目前這一輪劇本的即時狀態：手上該做什麼、下一個觸發點在哪。"""

from __future__ import annotations

from .bars import Bar
from .config import StrategyConfig
from .engine import Result, Trade


def render_status(result: Result, bars: list[Bar], cfg: StrategyConfig,
                  capital: float | None = None) -> str:
    last = bars[-1]
    out: list[str] = ["=" * 72,
                      f"部位現況（資料截至 {last.d}，指數收盤 {last.close:,.0f}）",
                      "=" * 72, ""]

    open_trades = [t for t in result.trades if t.exit_reason == "open"]
    if not open_trades:
        if result.setups:
            s = result.setups[-1]
            out.append("目前沒有進行中的部位。最近一次訊號：")
            out.append(f"  {s.describe()}")
            if result.trades:
                t = result.trades[-1]
                out.append(f"  該筆已於 {t.exit_date} 出場（{t.exit_reason}），報酬 {t.ret:+.1%}")
        else:
            out.append("目前沒有進行中的部位，資料期間內也沒有觸發過訊號。")
        out.append("")
        out.append("下一次進場條件：自高點回檔 ≥"
                   f"{cfg.setup.min_drawdown:.0%} 後，谷底起 ≤{cfg.setup.max_repair_bars} 個交易日內"
                   f"收盤補回 ≥{cfg.setup.repair_fraction:.0%}。")
        return "\n".join(out)

    t = open_trades[-1]
    lv = t.levels
    c = last.close
    zone = lv.zone(c)

    out.append(f"訊號        {t.setup.describe()}")
    out.append(f"已持有      {t.bars_held} 個交易日"
               f"（劇本有效期 {cfg.setup.setup_expiry_bars} 日）")
    out.append("")
    out.append(f"目標水位    {t.target_weight:.1%}"
               + (f"（約 {capital * t.target_weight:,.0f} 元）" if capital else ""))
    out.append(f"已建立      {t.filled_weight:.1%}"
               + (f"（約 {capital * t.filled_weight:,.0f} 元）" if capital else "")
               + f"　＝ 目標的 {t.filled_weight / t.target_weight:.0%}")
    out.append("")
    out.append("已成交")
    for f in t.fills:
        out.append("  " + f.describe())
    out.append("")

    zone_note = {
        "breakout": "已站上前高 —— 出場交給移動停利",
        "healthy": "劇本正常 —— 回檔就是加碼機會",
        "buffer": "主防線假跌破容忍區 —— 不加碼、也不減碼",
        "caution": "已跌破主防線 —— 停止加碼，只留現有部位",
        "warning": "已跌破警戒線 —— 減碼一半",
        "invalidated": "已跌破谷底 —— 劇本失效，清倉",
    }[zone]
    out.append(f"目前分區    {zone}　{zone_note}")
    out.append(f"距前高      {c / lv.peak - 1:+.1%}　"
               f"距主防線 {c / lv.half_line - 1:+.1%}　"
               f"距停損線 {c / lv.stop_line - 1:+.1%}　"
               f"距失效線 {c / lv.invalidation - 1:+.1%}")
    out.append("")

    out.append("下一步（收盤價判定，次一交易日執行）")
    if t.pending_ladder:
        for thr, w in t.pending_ladder:
            trigger_px = t.swing_high * (1 - thr)
            gap = trigger_px / c - 1
            blocked = trigger_px < lv.half_line_with_buffer
            note = "　⚠ 觸發價已低於假跌破緩衝線，屆時不執行" if blocked else ""
            amount = f"（約 {capital * w:,.0f} 元）" if capital else ""
            out.append(f"  買 {w:>6.1%}{amount}　收盤 ≤ {trigger_px:,.0f}"
                       f"（自波段高 {t.swing_high:,.0f} 回檔 {thr:.0%}，距現價 {gap:+.1%}）{note}")
        remaining = cfg.entry.fill_timeout_bars - t.bars_held
        if remaining > 0:
            out.append(f"  買 {'剩餘':>6}　再過 {remaining} 個交易日仍未觸發回檔梯 → 市價補齊")
        else:
            out.append(f"  買 {'剩餘':>6}　已過時間門檻 → 次一交易日市價補齊")
        if cfg.entry.breakout_fills_remainder:
            out.append(f"  買 {'剩餘':>6}　收盤 > {lv.peak:,.0f}（前高）→ 補齊，權重按停損距離縮放")
    else:
        out.append("  加碼梯已全部處理，不再加碼")

    if cfg.exit.target_take_fraction > 0:
        out.append(f"  賣 {cfg.exit.target_take_fraction:>6.0%}　收盤 ≥ {lv.peak:,.0f}（前高）")
    if t.reached_prior_high:
        out.append(f"  賣 {'全部':>6}　自波段最高收盤回檔 {cfg.exit.trail_drawdown:.0%}（移動停利已啟動）")
    else:
        out.append(f"  賣 {'全部':>6}　創高後啟動移動停利（自最高收盤回檔 {cfg.exit.trail_drawdown:.0%}）")
    note = ("警戒線" if lv.stop_line >= lv.warn_line
            else f"主防線 {lv.half_line:,.0f} 的 {cfg.levels.half_line_buffer:.1%} 緩衝")
    out.append(f"  賣 {cfg.exit.warn_derisk_fraction:>6.0%}　收盤 < {lv.stop_line:,.0f}（{note}）")
    out.append(f"  賣 {'全部':>6}　收盤 < {lv.invalidation:,.0f}（失效線）")
    out.append("")

    # 實際會把部位清光的第一條線：警戒線設定為全數出場時就是它，否則才是失效線
    full_exit = (lv.stop_line if cfg.exit.warn_derisk_fraction >= 1.0 else lv.invalidation)
    lev = cfg.sizing.leverage
    gap = full_exit / c - 1
    out.append(f"預期最大損失  跌到出清線 {full_exit:,.0f}（{gap:.1%}），"
               f"00631L 約 {lev * gap:.0%}；依已建立的 {t.filled_weight:.1%} 部位，"
               f"權益衝擊約 {t.filled_weight * lev * gap:.1%}")
    if full_exit != lv.invalidation:
        gap2 = lv.invalidation / c - 1
        out.append(f"              （跳空直接摜破失效線 {lv.invalidation:,.0f} 的極端情形："
                   f"{gap2:.1%}，權益衝擊約 {t.filled_weight * lev * gap2:.1%}）")
    return "\n".join(out)


def open_trade(result: Result) -> Trade | None:
    for t in reversed(result.trades):
        if t.exit_reason == "open":
            return t
    return None
